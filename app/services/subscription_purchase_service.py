import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PERIOD_PRICES, settings
from app.database.crud.server_squad import (
    add_user_to_servers,
    get_available_server_squads,
    get_server_ids_by_uuids,
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
from app.database.models import ServerSquad, Subscription, SubscriptionStatus, TransactionType, User
from app.localization.texts import get_texts
from app.services.subscription_service import SubscriptionService
from app.utils.pricing_utils import (
    calculate_months_from_days,
    format_period_description,
    validate_pricing_calculation,
)
from app.utils.promo_offer import get_user_active_promo_discount_percent
from app.utils.user_utils import mark_user_as_had_paid_subscription

logger = logging.getLogger(__name__)


@dataclass
class PurchaseTrafficOption:
    value: int
    label: str
    price_per_month: int
    price_label: str
    original_price_per_month: Optional[int] = None
    original_price_label: Optional[str] = None
    discount_percent: int = 0
    is_available: bool = True
    is_default: bool = False

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "value": self.value,
            "label": self.label,
            "price_kopeks": self.price_per_month,
            "price_label": self.price_label,
            "is_available": self.is_available,
        }
        if self.original_price_per_month is not None and (
            self.original_price_label and self.original_price_per_month != self.price_per_month
        ):
            payload["original_price_kopeks"] = self.original_price_per_month
            payload["original_price_label"] = self.original_price_label
        if self.discount_percent:
            payload["discount_percent"] = self.discount_percent
        if self.is_default:
            payload["is_default"] = True
        return payload


@dataclass
class PurchaseTrafficConfig:
    selectable: bool
    mode: str
    options: List[PurchaseTrafficOption] = field(default_factory=list)
    default_value: Optional[int] = None
    current_value: Optional[int] = None
    hint: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "selectable": self.selectable,
            "mode": self.mode,
        }
        if self.options:
            payload["options"] = [option.to_payload() for option in self.options]
        if self.default_value is not None:
            payload["default"] = self.default_value
        if self.current_value is not None:
            payload["current"] = self.current_value
        if self.hint:
            payload["hint"] = self.hint
        return payload


@dataclass
class PurchaseServerOption:
    uuid: str
    name: str
    price_per_month: int
    price_label: str
    original_price_per_month: Optional[int] = None
    original_price_label: Optional[str] = None
    discount_percent: int = 0
    is_available: bool = True

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "uuid": self.uuid,
            "name": self.name,
            "price_kopeks": self.price_per_month,
            "price_label": self.price_label,
            "is_available": self.is_available,
        }
        if self.original_price_per_month is not None and (
            self.original_price_label and self.original_price_per_month != self.price_per_month
        ):
            payload["original_price_kopeks"] = self.original_price_per_month
            payload["original_price_label"] = self.original_price_label
        if self.discount_percent:
            payload["discount_percent"] = self.discount_percent
        return payload


@dataclass
class PurchaseServersConfig:
    options: List[PurchaseServerOption]
    min_selectable: int
    max_selectable: int
    default_selection: List[str]
    hint: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "options": [option.to_payload() for option in self.options],
            "min": self.min_selectable,
            "max": self.max_selectable,
            "default": list(self.default_selection),
            "selected": list(self.default_selection),
        }
        if self.hint:
            payload["hint"] = self.hint
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
    original_price_label: Optional[str] = None
    discount_percent: int = 0
    hint: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "min": self.minimum,
            "max": self.maximum,
            "default": self.default,
            "current": self.current,
            "price_per_device_kopeks": self.discounted_price_per_device,
            "price_per_device_label": self.price_label,
        }
        if self.price_per_device and self.price_per_device != self.discounted_price_per_device:
            payload["price_per_device_original_kopeks"] = self.price_per_device
            if self.original_price_label:
                payload["price_per_device_original_label"] = self.original_price_label
        if self.discount_percent:
            payload["discount_percent"] = self.discount_percent
        if self.hint:
            payload["hint"] = self.hint
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
    base_price_original_label: Optional[str]
    discount_percent: int
    per_month_price: int
    per_month_price_label: str
    traffic: PurchaseTrafficConfig
    servers: PurchaseServersConfig
    devices: PurchaseDevicesConfig

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": self.id,
            "code": self.id,
            "period_id": self.id,
            "period_days": self.days,
            "period": self.days,
            "months": self.months,
            "label": self.label,
            "price_kopeks": self.base_price,
            "price_label": self.base_price_label,
            "per_month_price_kopeks": self.per_month_price,
            "per_month_price_label": self.per_month_price_label,
            "is_available": True,
            "traffic": self.traffic.to_payload(),
            "servers": self.servers.to_payload(),
            "devices": self.devices.to_payload(),
        }
        if self.discount_percent:
            payload["discount_percent"] = self.discount_percent
        if (
            self.base_price_original
            and self.base_price_original_label
            and self.base_price_original != self.base_price
        ):
            payload["original_price_kopeks"] = self.base_price_original
            payload["original_price_label"] = self.base_price_original_label
        return payload


@dataclass
class PurchaseSelection:
    period: PurchasePeriodConfig
    traffic_value: int
    servers: List[str]
    devices: int


@dataclass
class PurchasePricingResult:
    selection: PurchaseSelection
    server_ids: List[int]
    server_prices_for_period: List[int]
    base_original_total: int
    discounted_total: int
    promo_discount_value: int
    promo_discount_percent: int
    final_total: int
    months: int
    details: Dict[str, Any]


@dataclass
class PurchaseOptionsContext:
    user: User
    subscription: Optional[Subscription]
    currency: str
    balance_kopeks: int
    periods: List[PurchasePeriodConfig]
    default_period: PurchasePeriodConfig
    period_map: Dict[str, PurchasePeriodConfig]
    server_uuid_to_id: Dict[str, int]
    payload: Dict[str, Any]


class PurchaseValidationError(Exception):
    def __init__(self, message: str, code: str = "invalid_selection") -> None:
        super().__init__(message)
        self.code = code


class PurchaseBalanceError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


def _apply_percentage_discount(amount: int, percent: int) -> Tuple[int, int]:
    if amount <= 0 or percent <= 0:
        return amount, 0
    clamped = max(0, min(100, percent))
    discount_value = amount * clamped // 100
    discounted = amount - discount_value
    if discount_value >= 100 and discounted % 100:
        discounted += 100 - (discounted % 100)
        discounted = min(discounted, amount)
        discount_value = amount - discounted
    return discounted, discount_value


def _apply_discount_to_monthly_component(amount_per_month: int, percent: int, months: int) -> Dict[str, int]:
    discounted_per_month, discount_per_month = _apply_percentage_discount(amount_per_month, percent)
    return {
        "original_per_month": amount_per_month,
        "discounted_per_month": discounted_per_month,
        "discount_percent": max(0, min(100, percent)),
        "discount_per_month": discount_per_month,
        "total": discounted_per_month * months,
        "discount_total": discount_per_month * months,
    }


def _get_promo_offer_discount_percent(user: Optional[User]) -> int:
    return get_user_active_promo_discount_percent(user)


async def _apply_promo_offer_discount_with_fixed(
    db: AsyncSession,
    user: Optional[User],
    amount: int,
    is_subscription_only: bool = False
) -> Tuple[int, int, int]:
    """
    Применяет промо-скидку (процентную или фиксированную).

    Returns: (final_amount, discount_value, discount_percent)
    """
    if not user:
        return amount, 0, 0

    # Получаем процентную скидку
    percent = _get_promo_offer_discount_percent(user)

    # Проверяем активную одноразовую скидку
    from app.database.crud.discount_offer import get_latest_claimed_offer_for_user

    source = getattr(user, "promo_offer_discount_source", None)
    if source and source.startswith("promocode:"):
        try:
            offer = await get_latest_claimed_offer_for_user(db, user.id, source)
            if offer and offer.effect_type == "one_time_promo_discount":
                extra_data = getattr(offer, "extra_data", {}) or {}

                # Проверка области применения
                discount_applies_to = extra_data.get("discount_applies_to", "all")
                if discount_applies_to == "subscription_only" and not is_subscription_only:
                    # Скидка НЕ применяется (это не подписка)
                    return amount, 0, 0

                # Проверяем тип скидки
                discount_type = extra_data.get("discount_type")

                if discount_type == "fixed_amount":
                    # Фиксированная сумма
                    fixed_amount = extra_data.get("discount_value", 0)
                    if fixed_amount > 0:
                        discount_value = min(fixed_amount, amount)  # Не больше суммы
                        final_amount = max(0, amount - discount_value)
                        # Эквивалентный процент для отображения
                        equiv_percent = int(discount_value * 100 / amount) if amount > 0 else 0
                        return final_amount, discount_value, equiv_percent
        except Exception as e:
            logger.error(f"Ошибка применения одноразовой скидки: {e}")

    # Обычная процентная скидка
    if amount <= 0 or percent <= 0:
        return amount, 0, 0

    discounted, discount_value = _apply_percentage_discount(amount, percent)
    return discounted, discount_value, percent


def _apply_promo_offer_discount(user: Optional[User], amount: int) -> Tuple[int, int, int]:
    percent = _get_promo_offer_discount_percent(user)
    if amount <= 0 or percent <= 0:
        return amount, 0, 0
    discounted, discount_value = _apply_percentage_discount(amount, percent)
    return discounted, discount_value, percent


def _build_server_option(
    server: ServerSquad,
    discount_percent: int,
    texts,
) -> PurchaseServerOption:
    base_per_month = int(getattr(server, "price_kopeks", 0) or 0)
    discounted_per_month, _ = _apply_percentage_discount(base_per_month, discount_percent)
    return PurchaseServerOption(
        uuid=server.squad_uuid,
        name=getattr(server, "display_name", server.squad_uuid) or server.squad_uuid,
        price_per_month=discounted_per_month,
        price_label=texts.format_price(discounted_per_month),
        original_price_per_month=base_per_month,
        original_price_label=texts.format_price(base_per_month) if base_per_month != discounted_per_month else None,
        discount_percent=max(0, discount_percent),
        is_available=bool(getattr(server, "is_available", True) and not getattr(server, "is_full", False)),
    )


class MiniAppSubscriptionPurchaseService:
    """Builds configuration and pricing for subscription purchases in the mini app."""

    async def build_options(self, db: AsyncSession, user: User) -> PurchaseOptionsContext:
        from app.database.crud.subscription import get_subscription_by_user_id

        subscription = await get_subscription_by_user_id(db, user.id)
        balance_kopeks = int(getattr(user, "balance_kopeks", 0) or 0)
        currency = (getattr(user, "balance_currency", None) or "RUB").upper()
        texts = get_texts(getattr(user, "language", None))

        available_servers = await get_available_server_squads(
            db,
            promo_group_id=getattr(user, "promo_group_id", None),
        )
        server_catalog: Dict[str, ServerSquad] = {server.squad_uuid: server for server in available_servers}

        if subscription and subscription.connected_squads:
            for uuid in subscription.connected_squads:
                if uuid in server_catalog:
                    continue
                try:
                    existing = await get_server_squad_by_uuid(db, uuid)
                except Exception as error:  # pragma: no cover - defensive logging
                    logger.warning("Failed to load server squad %s: %s", uuid, error)
                    existing = None
                if existing:
                    server_catalog[uuid] = existing

        server_uuid_to_id: Dict[str, int] = {}
        for server in server_catalog.values():
            try:
                server_uuid_to_id[server.squad_uuid] = int(getattr(server, "id", 0) or 0)
            except (TypeError, ValueError):
                continue

        default_connected = list(getattr(subscription, "connected_squads", []) or [])
        if not default_connected:
            for server in available_servers:
                if getattr(server, "is_available", True) and not getattr(server, "is_full", False):
                    default_connected = [server.squad_uuid]
                    break

        available_periods: Sequence[int] = settings.get_available_subscription_periods()
        periods: List[PurchasePeriodConfig] = []
        period_map: Dict[str, PurchasePeriodConfig] = {}

        default_devices = settings.DEFAULT_DEVICE_LIMIT
        # Для триала НЕ используем его ограничения как дефолтные,
        # чтобы при продлении клиент получил стандартные значения платной подписки
        is_trial_subscription = subscription and getattr(subscription, "is_trial", False)
        if subscription and getattr(subscription, "device_limit", None) and not is_trial_subscription:
            default_devices = max(default_devices, int(subscription.device_limit))

        fixed_traffic_value = None
        if settings.is_traffic_fixed():
            fixed_traffic_value = settings.get_fixed_traffic_limit()
        elif subscription and subscription.traffic_limit_gb is not None and not is_trial_subscription:
            fixed_traffic_value = subscription.traffic_limit_gb

        default_period_days = available_periods[0] if available_periods else 30

        for period_days in available_periods:
            months = calculate_months_from_days(period_days)
            period_id = f"days:{period_days}"
            label = format_period_description(period_days, getattr(user, "language", "ru"))

            base_price_original = PERIOD_PRICES.get(period_days, 0)
            period_discount_percent = user.get_promo_discount("period", period_days)
            base_price, base_discount_total = _apply_percentage_discount(
                base_price_original, period_discount_percent
            )
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
            raise PurchaseValidationError("No subscription periods configured", code="configuration")

        default_period = period_map.get(f"days:{default_period_days}") or periods[0]

        default_selection = {
            "period_id": default_period.id,
            "periodId": default_period.id,
            "period_days": default_period.days,
            "periodDays": default_period.days,
            "traffic_value": default_period.traffic.current_value
            if default_period.traffic.current_value is not None
            else default_period.traffic.default_value,
            "trafficValue": default_period.traffic.current_value
            if default_period.traffic.current_value is not None
            else default_period.traffic.default_value,
            "servers": list(default_period.servers.default_selection),
            "countries": list(default_period.servers.default_selection),
            "server_uuids": list(default_period.servers.default_selection),
            "serverUuids": list(default_period.servers.default_selection),
            "devices": default_period.devices.current,
            "device_limit": default_period.devices.current,
            "deviceLimit": default_period.devices.current,
        }

        payload = {
            "currency": currency,
            "balance_kopeks": balance_kopeks,
            "balanceKopeks": balance_kopeks,
            "balance_label": texts.format_price(balance_kopeks),
            "balanceLabel": texts.format_price(balance_kopeks),
            "subscription_id": getattr(subscription, "id", None),
            "subscriptionId": getattr(subscription, "id", None),
            "periods": [period.to_payload() for period in periods],
            "traffic": default_period.traffic.to_payload(),
            "servers": default_period.servers.to_payload(),
            "devices": default_period.devices.to_payload(),
            "selection": default_selection,
            "summary": None,
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
        fixed_traffic_value: Optional[int],
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

        packages = [package for package in settings.get_traffic_packages() if package.get("enabled", True)]
        discount_percent = user.get_promo_discount("traffic", period_days)
        options: List[PurchaseTrafficOption] = []

        for package in packages:
            value = int(package.get("gb") or 0)
            price_per_month = int(package.get("price") or 0)
            discounted_per_month, discount_value = _apply_percentage_discount(price_per_month, discount_percent)
            label = texts.format_traffic(value if value else 0)
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
            mode="selectable",
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
        server_catalog: Dict[str, ServerSquad],
        default_selection: List[str],
    ) -> PurchaseServersConfig:
        discount_percent = user.get_promo_discount("servers", period_days)
        options: List[PurchaseServerOption] = []

        for uuid, server in server_catalog.items():
            option = _build_server_option(server, discount_percent, texts)
            options.append(option)

        if not options:
            default_selection = []

        return PurchaseServersConfig(
            options=options,
            min_selectable=1 if options else 0,
            max_selectable=len(options),
            default_selection=default_selection if default_selection else [opt.uuid for opt in options[:1]],
            hint=None,
        )

    def _build_devices_config(
        self,
        user: User,
        texts,
        period_days: int,
        default_devices: int,
    ) -> PurchaseDevicesConfig:
        discount_percent = user.get_promo_discount("devices", period_days)
        unit_price = settings.PRICE_PER_DEVICE
        discounted_unit_price, unit_discount_value = _apply_percentage_discount(unit_price, discount_percent)
        price_label = texts.format_price(discounted_unit_price)
        original_label = (
            texts.format_price(unit_price)
            if unit_discount_value and unit_price != discounted_unit_price
            else None
        )

        max_devices_setting = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else None
        if max_devices_setting is not None:
            maximum = max(max_devices_setting, default_devices)
        else:
            maximum = max(default_devices, settings.DEFAULT_DEVICE_LIMIT) + 10

        return PurchaseDevicesConfig(
            minimum=1,
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
        selection_payload: Dict[str, Any],
    ) -> PurchaseSelection:
        period_id = (
            selection_payload.get("period_id")
            or selection_payload.get("periodId")
            or selection_payload.get("period")
            or selection_payload.get("code")
        )
        if not period_id:
            period_days = selection_payload.get("period_days") or selection_payload.get("periodDays")
            if period_days is not None:
                period_id = f"days:{int(period_days)}"

        if not period_id or period_id not in context.period_map:
            raise PurchaseValidationError("Invalid or missing subscription period", code="invalid_period")

        period = context.period_map[period_id]

        traffic_value = (
            selection_payload.get("traffic_value")
            or selection_payload.get("trafficValue")
            or selection_payload.get("traffic")
            or selection_payload.get("traffic_gb")
            or selection_payload.get("trafficGb")
        )

        if period.traffic.selectable:
            available_values = {option.value for option in period.traffic.options}
            if traffic_value is None:
                traffic_value = period.traffic.current_value or period.traffic.default_value
            else:
                traffic_value = int(traffic_value)
                if available_values and traffic_value not in available_values:
                    raise PurchaseValidationError("Selected traffic option is not available", code="invalid_traffic")
        else:
            traffic_value = period.traffic.current_value or period.traffic.default_value or 0

        raw_servers: List[str] = []
        for key in ("servers", "countries", "server_uuids", "serverUuids"):
            value = selection_payload.get(key)
            if isinstance(value, list):
                raw_servers.extend(value)

        servers: List[str] = []
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
            raise PurchaseValidationError("Select at least one server", code="invalid_servers")

        if period.servers.max_selectable and len(servers) > period.servers.max_selectable:
            servers = servers[: period.servers.max_selectable]

        devices = (
            selection_payload.get("devices")
            or selection_payload.get("device_limit")
            or selection_payload.get("deviceLimit")
            or period.devices.current
            or period.devices.default
        )
        try:
            devices = int(devices)
        except (TypeError, ValueError):
            raise PurchaseValidationError("Invalid devices selection", code="invalid_devices")

        if devices < period.devices.minimum:
            devices = period.devices.minimum
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
        texts = get_texts(getattr(context.user, "language", None))
        months = selection.period.months

        server_ids = await get_server_ids_by_uuids(db, selection.servers)
        if len(server_ids) != len(selection.servers):
            raise PurchaseValidationError("Some selected servers are not available", code="invalid_servers")

        total_without_promo, details = await self._calculate_base_total(
            db,
            context.user,
            selection,
            server_ids,
        )

        base_original_total = (
            details["base_price_original"]
            + details["traffic_price_per_month"] * months
            + details["servers_price_per_month"] * months
            + details["devices_price_per_month"] * months
        )

        final_total, promo_discount_value, promo_percent = await _apply_promo_offer_discount_with_fixed(
            db, context.user, total_without_promo, is_subscription_only=True
        )

        discounted_total = total_without_promo

        is_valid = validate_pricing_calculation(
            details.get("base_price", 0),
            (
                details.get("traffic_price_per_month", 0)
                - details.get("traffic_discount_total", 0) // max(1, months)
            )
            + (
                details.get("servers_price_per_month", 0)
                - details.get("servers_discount_total", 0) // max(1, months)
            )
            + (
                details.get("devices_price_per_month", 0)
                - details.get("devices_discount_total", 0) // max(1, months)
            ),
            months,
            discounted_total,
        )

        if not is_valid:
            raise PurchaseValidationError("Failed to validate pricing", code="calculation_error")

        return PurchasePricingResult(
            selection=selection,
            server_ids=server_ids,
            server_prices_for_period=list(details.get("servers_individual_prices", [])),
            base_original_total=base_original_total,
            discounted_total=discounted_total,
            promo_discount_value=promo_discount_value,
            promo_discount_percent=promo_percent,
            final_total=final_total,
            months=months,
            details=details,
        )

    async def _calculate_base_total(
        self,
        db: AsyncSession,
        user: User,
        selection: PurchaseSelection,
        server_ids: List[int],
    ) -> Tuple[int, Dict[str, Any]]:
        from app.database.crud.subscription import calculate_subscription_total_cost

        total_cost, details = await calculate_subscription_total_cost(
            db,
            selection.period.days,
            selection.traffic_value,
            server_ids,
            selection.devices,
            user=user,
        )
        return total_cost, details

    def build_preview_payload(
        self,
        context: PurchaseOptionsContext,
        pricing: PurchasePricingResult,
    ) -> Dict[str, Any]:
        texts = get_texts(getattr(context.user, "language", None))
        details = pricing.details

        total_discount = pricing.base_original_total - pricing.final_total
        overall_discount_percent = 0
        if pricing.base_original_total > 0 and total_discount > 0:
            overall_discount_percent = int(round(total_discount * 100 / pricing.base_original_total))

        discount_lines: List[str] = []

        def build_discount_line(key: str, default: str, amount: int, percent: int) -> Optional[str]:
            if not amount:
                return None
            return texts.t(key, default).format(
                amount=texts.format_price(amount),
                percent=percent,
            )

        def build_discount_note(amount: int, percent: int) -> Optional[str]:
            if not amount:
                return None
            return texts.t(
                "MINIAPP_PURCHASE_BREAKDOWN_DISCOUNT_NOTE",
                "Discount: -{amount} ({percent}%)",
            ).format(
                amount=texts.format_price(amount),
                percent=percent,
            )

        base_discount_line = build_discount_line(
            "MINIAPP_PURCHASE_DISCOUNT_PERIOD",
            "Period discount: -{amount} ({percent}%)",
            details.get("base_discount_total", 0),
            details.get("base_discount_percent", 0),
        )
        if base_discount_line:
            discount_lines.append(base_discount_line)

        traffic_discount_line = build_discount_line(
            "MINIAPP_PURCHASE_DISCOUNT_TRAFFIC",
            "Traffic discount: -{amount} ({percent}%)",
            details.get("traffic_discount_total", 0),
            details.get("traffic_discount_percent", 0),
        )
        if traffic_discount_line:
            discount_lines.append(traffic_discount_line)

        servers_discount_line = build_discount_line(
            "MINIAPP_PURCHASE_DISCOUNT_SERVERS",
            "Servers discount: -{amount} ({percent}%)",
            details.get("servers_discount_total", 0),
            details.get("servers_discount_percent", 0),
        )
        if servers_discount_line:
            discount_lines.append(servers_discount_line)

        devices_discount_line = build_discount_line(
            "MINIAPP_PURCHASE_DISCOUNT_DEVICES",
            "Devices discount: -{amount} ({percent}%)",
            details.get("devices_discount_total", 0),
            details.get("devices_discount_percent", 0),
        )
        if devices_discount_line:
            discount_lines.append(devices_discount_line)

        promo_discount_line = None
        if pricing.promo_discount_value:
            promo_discount_line = texts.t(
                "MINIAPP_PURCHASE_DISCOUNT_PROMO",
                "Promo offer: -{amount} ({percent}%)",
            ).format(
                amount=texts.format_price(pricing.promo_discount_value),
                percent=pricing.promo_discount_percent,
            )
            discount_lines.append(promo_discount_line)

        breakdown = [
            {
                "label": texts.t(
                    "MINIAPP_PURCHASE_BREAKDOWN_BASE",
                    "Base plan",
                ),
                "value": texts.format_price(details.get("base_price", 0)),
            }
        ]

        base_discount_note = build_discount_note(
            details.get("base_discount_total", 0),
            details.get("base_discount_percent", 0),
        )
        if base_discount_note:
            breakdown[0]["discount_label"] = base_discount_note
            breakdown[0]["discountLabel"] = base_discount_note

        if details.get("total_traffic_price"):
            traffic_item = {
                "label": texts.t(
                    "MINIAPP_PURCHASE_BREAKDOWN_TRAFFIC",
                    "Traffic",
                ),
                "value": texts.format_price(details["total_traffic_price"]),
            }
            traffic_discount_note = build_discount_note(
                details.get("traffic_discount_total", 0),
                details.get("traffic_discount_percent", 0),
            )
            if traffic_discount_note:
                traffic_item["discount_label"] = traffic_discount_note
                traffic_item["discountLabel"] = traffic_discount_note
            breakdown.append(traffic_item)

        if details.get("total_servers_price"):
            servers_item = {
                "label": texts.t(
                    "MINIAPP_PURCHASE_BREAKDOWN_SERVERS",
                    "Servers",
                ),
                "value": texts.format_price(details["total_servers_price"]),
            }
            servers_discount_note = build_discount_note(
                details.get("servers_discount_total", 0),
                details.get("servers_discount_percent", 0),
            )
            if servers_discount_note:
                servers_item["discount_label"] = servers_discount_note
                servers_item["discountLabel"] = servers_discount_note
            breakdown.append(servers_item)

        if details.get("total_devices_price"):
            devices_item = {
                "label": texts.t(
                    "MINIAPP_PURCHASE_BREAKDOWN_DEVICES",
                    "Devices",
                ),
                "value": texts.format_price(details["total_devices_price"]),
            }
            devices_discount_note = build_discount_note(
                details.get("devices_discount_total", 0),
                details.get("devices_discount_percent", 0),
            )
            if devices_discount_note:
                devices_item["discount_label"] = devices_discount_note
                devices_item["discountLabel"] = devices_discount_note
            breakdown.append(devices_item)

        if pricing.promo_discount_value:
            promo_item = {
                "label": texts.t(
                    "MINIAPP_PURCHASE_BREAKDOWN_PROMO",
                    "Promo discount",
                ),
                "value": f"- {texts.format_price(pricing.promo_discount_value)}",
            }
            if promo_discount_line:
                promo_item["discount_label"] = promo_discount_line
                promo_item["discountLabel"] = promo_discount_line
            breakdown.append(promo_item)

        missing = max(0, pricing.final_total - context.balance_kopeks)
        status_message = ""
        if missing > 0:
            status_message = texts.t(
                "MINIAPP_PURCHASE_STATUS_INSUFFICIENT",
                "Not enough funds on balance",
            )

        per_month_price = pricing.final_total // pricing.months if pricing.months else pricing.final_total

        return {
            "total_price_kopeks": pricing.final_total,
            "totalPriceKopeks": pricing.final_total,
            "total_price_label": texts.format_price(pricing.final_total),
            "totalPriceLabel": texts.format_price(pricing.final_total),
            "original_price_kopeks": pricing.base_original_total if total_discount else None,
            "originalPriceKopeks": pricing.base_original_total if total_discount else None,
            "original_price_label": texts.format_price(pricing.base_original_total)
            if total_discount
            else None,
            "originalPriceLabel": texts.format_price(pricing.base_original_total)
            if total_discount
            else None,
            "discount_percent": overall_discount_percent,
            "discountPercent": overall_discount_percent,
            "discount_label": texts.t(
                "MINIAPP_PURCHASE_SUMMARY_DISCOUNT",
                "You save {amount}",
            ).format(amount=texts.format_price(total_discount))
            if total_discount
            else None,
            "discountLabel": texts.t(
                "MINIAPP_PURCHASE_SUMMARY_DISCOUNT",
                "You save {amount}",
            ).format(amount=texts.format_price(total_discount))
            if total_discount
            else None,
            "discount_lines": discount_lines,
            "discountLines": discount_lines,
            "per_month_price_kopeks": per_month_price,
            "perMonthPriceKopeks": per_month_price,
            "per_month_price_label": texts.format_price(per_month_price),
            "perMonthPriceLabel": texts.format_price(per_month_price),
            "breakdown": [
                {"label": item["label"], "value": item["value"]}
                for item in breakdown
            ],
            "balance_kopeks": context.balance_kopeks,
            "balanceKopeks": context.balance_kopeks,
            "balance_label": texts.format_price(context.balance_kopeks),
            "balanceLabel": texts.format_price(context.balance_kopeks),
            "missing_amount_kopeks": missing,
            "missingAmountKopeks": missing,
            "missing_amount_label": texts.format_price(missing) if missing else None,
            "missingAmountLabel": texts.format_price(missing) if missing else None,
            "can_purchase": missing == 0,
            "canPurchase": missing == 0,
            "status_message": status_message,
            "statusMessage": status_message,
        }

    async def submit_purchase(
        self,
        db: AsyncSession,
        context: PurchaseOptionsContext,
        pricing: PurchasePricingResult,
    ) -> Dict[str, Any]:
        user = context.user
        texts = get_texts(getattr(user, "language", None))

        if pricing.final_total <= 0:
            raise PurchaseValidationError("Invalid total amount", code="calculation_error")

        if user.balance_kopeks < pricing.final_total:
            raise PurchaseBalanceError(
                texts.t(
                    "MINIAPP_PURCHASE_STATUS_INSUFFICIENT",
                    "Not enough funds on balance",
                )
            )

        description = f"Покупка подписки на {pricing.selection.period.days} дней"
        success = await subtract_user_balance(
            db,
            user,
            pricing.final_total,
            description,
            consume_promo_offer=pricing.promo_discount_value > 0,
        )
        if not success:
            raise PurchaseBalanceError(
                texts.t(
                    "MINIAPP_PURCHASE_STATUS_INSUFFICIENT",
                    "Not enough funds on balance",
                )
            )

        await db.refresh(user)

        subscription = context.subscription
        if subscription is not None and getattr(subscription, "id", None):
            try:
                await db.refresh(subscription)
            except Exception as refresh_error:  # pragma: no cover - defensive logging
                logger.warning(
                    "Failed to refresh existing subscription %s: %s",
                    getattr(subscription, "id", None),
                    refresh_error,
                )
        else:
            result = await db.execute(
                select(Subscription).where(Subscription.user_id == user.id)
            )
            subscription = result.scalar_one_or_none()
            if subscription is not None:
                context.subscription = subscription

        was_trial_conversion = False
        now = datetime.utcnow()

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
                        payment_method="balance",
                        first_payment_amount_kopeks=pricing.final_total,
                        first_paid_period_days=pricing.selection.period.days,
                    )
                except Exception as conversion_error:  # pragma: no cover - defensive logging
                    logger.error("Failed to create subscription conversion record: %s", conversion_error)

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

        await mark_user_as_had_paid_subscription(db, user)

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
                logger.error("Failed to register subscription servers: %s", error)

        subscription_service = SubscriptionService()
        try:
            if getattr(user, "remnawave_uuid", None):
                await subscription_service.update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
                    reset_reason="miniapp purchase",
                )
            else:
                await subscription_service.create_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
                    reset_reason="miniapp purchase",
                )
        except Exception as remnawave_error:  # pragma: no cover - defensive logging
            logger.error("Failed to sync subscription with RemnaWave: %s", remnawave_error)

        transaction = await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=pricing.final_total,
            description=f"Подписка на {pricing.selection.period.days} дней ({pricing.months} мес)",
        )

        await db.refresh(user)
        await db.refresh(subscription)

        message = texts.t(
            "SUBSCRIPTION_PURCHASED",
            "🎉 Subscription purchased successfully!",
        )

        if pricing.promo_discount_value:
            note = texts.t(
                "SUBSCRIPTION_PROMO_DISCOUNT_NOTE",
                "⚡ Extra discount {percent}%: -{amount}",
            ).format(
                percent=pricing.promo_discount_percent,
                amount=texts.format_price(pricing.promo_discount_value),
            )
            message = f"{message}\n\n{note}"

        return {
            "subscription": subscription,
            "transaction": transaction,
            "was_trial_conversion": was_trial_conversion,
            "message": message,
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
        total_price_kopeks: int
    ):
        """Creates a subscription order with predefined parameters."""
        from app.database.crud.subscription import create_pending_subscription
        from app.database.models import SubscriptionStatus
        
        # Create a pending subscription
        subscription = await create_pending_subscription(
            db=db,
            user_id=user_id,
            duration_days=period_days,
            traffic_limit_gb=traffic_limit_gb,
            device_limit=device_limit,
            connected_squads=[squad_uuid] if squad_uuid else [],
            payment_method=payment_method,
            total_price_kopeks=total_price_kopeks
        )
        
        return subscription


purchase_service = MiniAppSubscriptionPurchaseService()
