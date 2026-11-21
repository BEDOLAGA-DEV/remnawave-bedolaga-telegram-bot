"""Публичное продление подписки по веб-ссылке (без Telegram, без входа в кабинет).

Ссылка вида ``/cabinet/public/renew/{short_uuid}`` (за прокси кабинета — ``/api/cabinet/...``)
открывается в обычном браузере: человек выбирает период и способ оплаты, платит через шлюз,
а подписку продлевает вебхук шлюза (см. ``guest_purchase_service._fulfill_renew_purchase``).

Идентификатор в ссылке — ``Subscription.remnawave_short_uuid``. Он же единственный «секрет»:
ни логина, ни JWT здесь нет. Поэтому страница показывает только то, что и так знает владелец
ссылки (название тарифа, дату окончания, цены периодов, способы оплаты) и НИКОГДА — ссылку
подписки, конфиг, email, баланс или имя пользователя.

Как и ``unsubscribe.py``: страницы рендерятся на сервере, самодостаточны (без внешних ресурсов),
``GET`` ничего не меняет (его дёргают предпросмотрщики ссылок в мессенджерах), состояние меняет
только ``POST``.

Цену считает сервер (``pricing_engine.calculate_renewal_price`` — как в боте: скидка промогруппы +
разовое промо-предложение). Сумма от клиента не принимается вообще: из формы читаются только
номер периода и идентификатор способа оплаты.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.ip_utils import get_client_ip
from app.config import settings
from app.database.crud.landing import get_active_landing_by_slug, get_purchase_by_token
from app.database.crud.subscription import get_subscription_by_id, get_subscription_by_remnawave_short_uuid
from app.database.crud.tariff import get_tariff_by_id
from app.database.models import GuestPurchaseStatus, Subscription, SubscriptionStatus, UserStatus
from app.services.guest_purchase_service import (
    GuestPurchaseError,
    create_purchase,
    is_telegram_only_payment_method,
    resolve_landing_payment_method,
    validate_payment_amount_limits,
)
from app.services.payment_method_config_service import _get_method_defaults
from app.services.payment_service import PaymentService
from app.services.pricing_engine import RenewalPricing, pricing_engine
from app.utils.cache import RateLimitCache
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/public', tags=['Cabinet:Public'])

# Слаг лендинг-конфига, из которого берётся список способов оплаты для продления.
# Сам лендинг через /landing/renew покупать ничего не должен: оставьте allowed_tariff_ids пустым.
RENEW_LANDING_SLUG = 'renew'

# Апселл: триал (или подписка на выключенном тарифе) продлить своим тарифом нельзя —
# ведём на этот платный тариф (⭐Стандарт id=2). Чтобы сменить цель, поменяйте id здесь.
RENEW_UPSELL_TARIFF_ID = 2

# Кнопки на странице «ссылка недоступна» / ошибках (в настройках бота этих ссылок нет).
_SUPPORT_TG_URL = 'https://t.me/SligVPNHelp'
_SITE_URL = 'https://web.slig.app'

# Период-«хит»: помечается бейджем ХИТ (если есть среди периодов).
RENEW_HIT_PERIOD_DAYS = 180
# Период, выбранный по умолчанию при открытии страницы (если есть среди периодов).
RENEW_DEFAULT_PERIOD_DAYS = 90

# Публичный путь эндпоинтов за прокси кабинета (как у отписки: CABINET_URL + /api/cabinet/public/...).
PUBLIC_RENEW_PATH = '/api/cabinet/public/renew'

# short_uuid Remnawave — короткий url-safe идентификатор. Всё остальное отсекаем до похода в БД.
_SHORT_UUID_RE = re.compile(r'^[A-Za-z0-9_-]{4,64}$')
_METHOD_RE = re.compile(r'^[a-z0-9_]{1,50}$')

# Источник клиента для сплита продлений (Happ vs Incy). Приходит в ?app=, пишется
# в GuestPurchase.campaign_slug как 'app_<name>'. Белый список — всё прочее игнорируем.
_ALLOWED_APPS = {'happ', 'incy'}


def _app_source(value: str | None) -> str:
    """Нормализует ?app= к 'happ'/'incy' (или '' вне белого списка)."""
    v = (value or '').strip().lower()
    return v if v in _ALLOWED_APPS else ''

_MAX_FORM_BYTES = 4096
# Страница статуса сама обновляется, пока покупка свежая; потом перестаёт.
_STATUS_REFRESH_WINDOW = timedelta(minutes=30)

_NOT_AVAILABLE_TITLE = 'Ссылка недоступна'
_NOT_AVAILABLE_TEXT = (
    'Ссылка недействительна, либо продление для этой подписки по ссылке недоступно. '
    'Обратитесь в поддержку сервиса.'
)


# ============ Rendering ============


def _service_name() -> str:
    return html.escape(settings.SMTP_FROM_NAME or 'VPN')


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(n)
    if 11 <= n_abs % 100 <= 14:
        return many
    last = n_abs % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _period_label(days: int) -> str:
    if days >= 365 and days % 365 == 0:
        years = days // 365
        return f'{years} {_ru_plural(years, "год", "года", "лет")}'
    if days >= 30 and days % 30 == 0:
        months = days // 30
        return f'{months} {_ru_plural(months, "месяц", "месяца", "месяцев")}'
    return f'{days} {_ru_plural(days, "день", "дня", "дней")}'


_CSS = """
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:#0a0e0d;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#e8efec;-webkit-font-smoothing:antialiased}
.card{max-width:460px;margin:12px auto 20px;padding:18px 20px 16px;background:#141917;border:1px solid #232b28;border-top:3px solid var(--accent,#25c268);border-radius:18px;box-shadow:0 18px 48px rgba(0,0,0,.36)}
h1{margin:0 0 4px;font-size:22px;font-weight:600;color:#fff;letter-spacing:-.3px}
h2{margin:18px 0 8px;font-size:13px;font-weight:500;color:#8b9995}
p{margin:0;font-size:15px;line-height:1.55}
.muted{color:#8b9995;font-size:13px}
.banner{margin:14px 0 2px;padding:11px 13px;border-radius:12px;background:rgba(37,194,104,.09);border:1px solid rgba(37,194,104,.22);color:#8fe9b0;font-size:13px;font-weight:500}
.opt{display:flex;align-items:center;gap:12px;padding:12px 13px;margin:0 0 8px;border:1px solid #232b28;border-radius:13px;cursor:pointer;font-size:15px;transition:border-color .15s,background .15s}
.opt:hover{border-color:#2f4a3f}
.opt:has(input:checked){border-color:#25c268;background:rgba(37,194,104,.07)}
.opt input{accent-color:#25c268;width:18px;height:18px;margin:0;flex:none}
.opt .label{display:flex;flex-direction:column;gap:2px;min-width:0}
.opt .label .sub{font-size:12px;color:#8b9995}
.opt .price{margin-left:auto;text-align:right;font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}
.opt .price s{display:block;color:#6f7b77;font-weight:400;font-size:12px}
.badge{display:inline-block;margin-left:8px;padding:2px 7px;border-radius:7px;background:rgba(37,194,104,.14);color:#64dd98;font-size:11px;font-weight:500;vertical-align:middle}
.badge-hit{display:inline-block;margin-left:8px;padding:2px 8px;border-radius:7px;background:#25c268;color:#06120b;font-size:11px;font-weight:600;vertical-align:middle}
.opt .label .save{font-size:12px;color:#64dd98;font-weight:500}
button{width:100%;margin-top:16px;border:0;border-radius:12px;padding:14px 20px;font-size:16px;font-weight:600;color:#06120b;background:#25c268;cursor:pointer}
button:hover{background:#2bd275}
.pm-icon{flex:none;width:26px;height:26px;display:grid;place-items:center;color:#8b9995}
.pm-icon svg{width:22px;height:22px;display:block}
.btn-row{display:flex;gap:10px;margin-top:20px}
.btn-sec{flex:1;text-align:center;text-decoration:none;padding:12px 10px;border-radius:11px;border:1px solid #2a322e;color:#c7d2cd;font-size:14px;font-weight:500;background:rgba(255,255,255,.02)}
.btn-sec:hover{background:rgba(255,255,255,.05);border-color:#3a443f}
.foot{margin:20px 0 0;font-size:12px;color:#5f6b67;text-align:center}
"""


def _html_response(
    title: str,
    inner: str,
    *,
    status_code: int = 200,
    accent: str = '#25c268',
    refresh_seconds: int | None = None,
) -> HTMLResponse:
    refresh = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">' if refresh_seconds else ''
    body = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<meta name="referrer" content="no-referrer">
{refresh}
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
  <div class="card" style="--accent:{accent}">
    {inner}
    <p class="foot">&copy; {_service_name()}</p>
  </div>
</body>
</html>"""
    # URL содержит секрет (short_uuid / токен покупки): не кэшируем и не отдаём в Referer.
    return HTMLResponse(
        body,
        status_code=status_code,
        headers={
            'Cache-Control': 'no-store',
            'Referrer-Policy': 'no-referrer',
            'X-Robots-Tag': 'noindex, nofollow',
            'X-Frame-Options': 'DENY',
        },
    )


def _message_page(title: str, message: str, *, status_code: int, ok: bool = False) -> HTMLResponse:
    # Кнопки «поддержка/сайт» — на недоступности (404) и серверных ошибках (5xx),
    # где человек упёрся и ему нужен выход; на валидации (400) и успехе не показываем.
    extra = _support_buttons() if (not ok and (status_code == 404 or status_code >= 500)) else ''
    return _html_response(
        title,
        f'<h1>{html.escape(title)}</h1><p style="margin-top:10px">{html.escape(message)}</p>{extra}',
        status_code=status_code,
        accent='#16a34a' if ok else '#dc2626',
    )


def _not_available_page() -> HTMLResponse:
    # Один и тот же ответ для «нет такой подписки» и «подписку нельзя продлить»:
    # иначе эндпоинт стал бы оракулом существования short_uuid.
    return _message_page(_NOT_AVAILABLE_TITLE, _NOT_AVAILABLE_TEXT, status_code=404)


def _rate_limited_page() -> HTMLResponse:
    return _message_page('Слишком много запросов', 'Подождите минуту и попробуйте снова.', status_code=429)


# ============ Guards / helpers ============


def _public_base_url(request: Request) -> str:
    """Базовый URL публичного эндпоинта для return_url платёжки."""
    cabinet_url = settings._normalized_cabinet_url()
    if cabinet_url:
        return f'{cabinet_url}{PUBLIC_RENEW_PATH}'
    # CABINET_URL не настроен: лучше что-то осмысленное, чем сломанный return_url.
    return f'{str(request.base_url).rstrip("/")}/cabinet/public/renew'


def _subject_digest(short_uuid: str) -> str:
    """Непрозрачный дайджест для ключа лимита: сам short_uuid в Redis/логи не кладём."""
    return hashlib.sha256(short_uuid.encode()).hexdigest()[:32]


async def _is_rate_limited(
    request: Request,
    short_uuid: str | None,
    action: str,
    *,
    ip_limit: int,
    sub_limit: int,
    window: int,
) -> bool:
    """True, если упёрлись в лимит по IP или по подписке. Fail-closed при недоступном Redis."""
    try:
        client_ip = get_client_ip(request)
    except HTTPException:
        return True
    if await RateLimitCache.is_ip_rate_limited(client_ip, action, limit=ip_limit, window=window, fail_closed=True):
        return True
    if short_uuid and await RateLimitCache.is_subject_rate_limited(
        _subject_digest(short_uuid), action, limit=sub_limit, window=window, fail_closed=True
    ):
        return True
    return False


def _should_upsell(subscription: Subscription | None) -> bool:
    """Продлить СВОИМ тарифом нельзя → ведём на платный (апселл).

    True для триалов и для подписок на выключенном тарифе: их собственный тариф
    либо триальный, либо снят с продажи, и цена периода выходит 0 (страница 503).
    Таких ведём покупать платный ``RENEW_UPSELL_TARIFF_ID``.
    """
    if subscription is None:
        return False
    if subscription.is_trial:
        return True
    tariff = subscription.tariff if subscription.tariff_id else None
    return tariff is not None and not tariff.is_active


def _renewal_block_reason(subscription: Subscription | None, *, for_upsell: bool = False) -> str | None:
    """Почему подписку нельзя продлить/оформить по ссылке (None = можно).

    Повторяет проверки ``subscription_modules/renewal.py`` + суточные тарифы и
    подписки заблокированных/удалённых пользователей. Причина уходит только в лог,
    клиенту — всегда одинаковая страница (см. ``_not_available_page``).

    ``for_upsell``: при апселле проверки ТЕКУЩЕГО тарифа пропускаются (цена и период
    берутся от целевого платного тарифа, см. ``_upsell_period_options``) — остаются
    только общие блокировки по пользователю и статусу подписки.
    """
    if subscription is None:
        return 'subscription_not_found'
    user = subscription.user
    if user is None:
        return 'no_user'
    if user.status != UserStatus.ACTIVE.value:
        return 'user_not_active'
    if getattr(user, 'restriction_subscription', False):
        return 'user_restricted'

    non_renewable = {SubscriptionStatus.DISABLED.value, SubscriptionStatus.PENDING.value}
    actual_status = getattr(subscription, 'actual_status', subscription.status)
    if actual_status in non_renewable:
        return f'status_{actual_status}'

    if for_upsell:
        # Текущий тариф не важен — покупается целевой платный, доступность которого
        # проверяется отдельно в хендлере.
        return None

    if subscription.is_trial and not subscription.tariff_id:
        return 'trial_without_tariff'

    # Classic subscriptions cannot be renewed when tariff mode is enabled
    if settings.is_tariffs_mode() and not subscription.tariff_id:
        return 'classic_in_tariffs_mode'

    # Суточные тарифы продлеваются посуточным списанием с баланса, а не оплатой периода.
    tariff = subscription.tariff if subscription.tariff_id else None
    if tariff is not None and getattr(tariff, 'is_daily', False):
        return 'daily_tariff'

    return None


def _renewal_periods(subscription: Subscription) -> list[int]:
    """Периоды продления — те же, что в кабинете (``get_renewal_options``)."""
    tariff = subscription.tariff if subscription.tariff_id else None
    if tariff and tariff.is_active and tariff.period_prices:
        return sorted(int(k) for k in tariff.period_prices.keys())
    return settings.get_available_renewal_periods()


async def _price_period(db: AsyncSession, subscription: Subscription, period_days: int) -> RenewalPricing | None:
    """Цена продления «как в боте» или None, если период оплатить нельзя."""
    try:
        pricing = await pricing_engine.calculate_renewal_price(db, subscription, period_days, user=subscription.user)
    except ValueError:
        return None
    # Шлюз не примет нулевую сумму: бесплатные периоды по ссылке не продлеваем.
    if pricing.final_total <= 0:
        return None
    return pricing


async def _period_options(db: AsyncSession, subscription: Subscription) -> list[tuple[int, RenewalPricing]]:
    options: list[tuple[int, RenewalPricing]] = []
    for period in _renewal_periods(subscription):
        pricing = await _price_period(db, subscription, period)
        if pricing is not None:
            options.append((period, pricing))
    return options


def _upsell_periods(tariff) -> list[int]:
    """Периоды, которые можно купить у целевого тарифа (без суточных)."""
    try:
        periods = [int(p) for p in tariff.get_purchasable_periods()]
    except Exception:
        periods = sorted(int(k) for k in (tariff.period_prices or {}).keys())
    return sorted(p for p in periods if p > 0)


async def _upsell_price_period(tariff, period_days: int, user) -> RenewalPricing | None:
    """Цена покупки целевого тарифа «как в боте» или None, если период оплатить нельзя."""
    try:
        pricing = await pricing_engine.calculate_tariff_purchase_price(tariff, period_days, user=user)
    except ValueError:
        return None
    if pricing.final_total <= 0:
        return None
    return pricing


async def _upsell_period_options(tariff, user) -> list[tuple[int, RenewalPricing]]:
    """Платные периоды целевого тарифа для апселла (цена через calculate_tariff_purchase_price)."""
    options: list[tuple[int, RenewalPricing]] = []
    for period in _upsell_periods(tariff):
        pricing = await _upsell_price_period(tariff, period, user)
        if pricing is not None:
            options.append((period, pricing))
    return options


# Чистые линейные иконки способов оплаты (currentColor), вместо эмодзи в названиях.
_PM_ICONS = {
    'sbp': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.4"/><rect x="3" y="14" width="7" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="7" rx="1.4"/><path d="M14 14h3.6v3.6M21 14.4V21h-6.6"/></svg>',
    'card': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="5" width="19" height="14" rx="2.6"/><path d="M2.5 9.6h19"/><path d="M6 15h4"/></svg>',
    'crypto': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.6"/><path d="M10 7.9v8.2M12.3 7.9v8.2" stroke-width="1.3"/><path d="M9 9h4.3a1.9 1.9 0 010 3.7H9M9 12.4h4.7a1.9 1.9 0 010 3.8H9" stroke-width="1.3"/></svg>',
    'other': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="18" height="13" rx="2.6"/><path d="M3 10h18M16 14.5h2.2"/></svg>',
}


def _strip_lead_emoji(text: str) -> str:
    """Убрать ведущие эмодзи/символы и пробелы из названия способа оплаты."""
    s = str(text)
    cleaned = re.sub(r'^[^0-9A-Za-zА-Яа-яЁё(]+', '', s).strip()
    return cleaned or s.strip()


def _payment_kind(value: str, label: str) -> str:
    """Тип способа оплаты для иконки."""
    v, lab = value.lower(), label.lower()
    if 'сбп' in lab or 'sbp' in v or 'qr' in lab:
        return 'sbp'
    if 'крипт' in lab or 'crypto' in lab or any(x in v for x in ('cryptomus', 'heleket', 'crypto')):
        return 'crypto'
    if 'карт' in lab or 'card' in v or 'card' in lab:
        return 'card'
    return 'other'


def _support_buttons() -> str:
    return (
        '<div class="btn-row">'
        f'<a class="btn-sec" href="{html.escape(_SUPPORT_TG_URL, quote=True)}">Поддержка в Telegram</a>'
        f'<a class="btn-sec" href="{html.escape(_SITE_URL, quote=True)}">На сайт</a>'
        '</div>'
    )


def _payment_options(raw_methods: list[dict] | None) -> list[tuple[str, str, str]]:
    """Список (значение формы, подпись, тип-для-иконки) из конфига лендинга.

    Под-опции (СБП/карта/банк и т.п.) разворачиваются в отдельные пункты со значением
    ``<method_id>_<sub_option_id>`` — ровно так их шлёт и обычный лендинг. Подпись для метода
    с под-опциями — имя самой под-опции (``СБП (QR)``, ``Карты (RUB)`` — из настроек платёжки),
    без под-опций — ``display_name`` метода (правит владелец в админке). Эмодзи из названий
    убираются — слева рисуется линейная иконка. Telegram-only методы пропускаются.
    """
    method_defaults = _get_method_defaults()
    options: list[tuple[str, str, str]] = []
    for method in sorted(raw_methods or [], key=lambda m: m.get('sort_order', 0) or 0):
        method_id = str(method.get('method_id') or '')
        if not method_id:
            continue
        name = str(method.get('display_name') or method_id)
        raw_sub_options = method.get('sub_options')  # dict[str, bool] | None

        entries: list[tuple[str, str]] = []  # (value, label)
        method_def = method_defaults.get(method_id)
        available = method_def.get('available_sub_options') if method_def else None
        if available:
            for opt in available:
                if raw_sub_options is None or raw_sub_options.get(opt['id'], True):
                    entries.append((f'{method_id}_{opt["id"]}', str(opt['name'])))
        if not entries:
            entries.append((method_id, name))

        for value, label in entries:
            if is_telegram_only_payment_method(value):
                continue
            clean = _strip_lead_emoji(label)
            options.append((value, clean, _payment_kind(value, clean)))
    return options


def _expiry_text(subscription: Subscription) -> str:
    end_text = format_local_datetime(subscription.end_date, '%d.%m.%Y %H:%M')
    if subscription.is_expired:
        return f'Подписка закончилась {end_text}'
    return f'Подписка действует до {end_text}'


def _price_html(pricing: RenewalPricing) -> str:
    price = html.escape(settings.format_price(pricing.final_total))
    if pricing.original_total > pricing.final_total:
        return f'<s>{html.escape(settings.format_price(pricing.original_total))}</s>{price}'
    return price


def _discount_pct(pricing: RenewalPricing) -> int:
    """Процент скидки клиента на этом периоде (0 — если скидки нет)."""
    original = pricing.original_total
    final = pricing.final_total
    if original > final > 0:
        return round((original - final) / original * 100)
    return 0


def _period_savings(periods: list[tuple[int, RenewalPricing]]) -> dict[int, int]:
    """Выгода длинного срока: насколько дешевле выходит месяц против самого дорогого
    (обычно самого короткого) срока. {период_дней: процент_выгоды}, 0 — если выгоды нет.
    """
    rates: dict[int, float] = {}
    for days, pricing in periods:
        months = days / 30.0
        if months > 0 and pricing.final_total > 0:
            rates[days] = pricing.final_total / months
    if not rates:
        return {}
    base = max(rates.values())  # самый дорогой месяц — у самого короткого срока
    out: dict[int, int] = {}
    for days, rate in rates.items():
        pct = round((base - rate) / base * 100) if base > 0 else 0
        out[days] = pct if pct > 0 else 0
    return out


def _hit_badge(days: int) -> str:
    return '<span class="badge-hit">Хит</span>' if days == RENEW_HIT_PERIOD_DAYS else ''


def _discount_banner(periods: list[tuple[int, RenewalPricing]]) -> str:
    """Баннер с максимальной действующей скидкой клиента (promo-группа + предложение)."""
    best = max((_discount_pct(p) for _, p in periods), default=0)
    if best <= 0:
        return ''
    return f'<div class="banner">Ваша скидка до {best}% уже учтена в ценах ниже</div>'


# ============ Routes ============


@router.get('/renew/status/{token}', summary='Статус оплаты продления (страница возврата из платёжки)')
async def renew_status_page(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
) -> HTMLResponse:
    """Только читает. Токен покупки — случайная строка на 64 символа, выдаётся лишь тому, кто начал оплату."""
    if await _is_rate_limited(request, None, 'public_renew_status', ip_limit=30, sub_limit=0, window=60):
        return _rate_limited_page()

    purchase = await get_purchase_by_token(db, token) if 8 <= len(token) <= 128 else None
    if purchase is None or purchase.source != 'renew':
        return _not_available_page()

    status_value = purchase.status
    if status_value == GuestPurchaseStatus.DELIVERED.value:
        end_text = ''
        if purchase.renew_subscription_id:
            subscription = await get_subscription_by_id(db, purchase.renew_subscription_id)
            if subscription is not None:
                end_text = f' Подписка действует до {format_local_datetime(subscription.end_date, "%d.%m.%Y %H:%M")}.'
        return _message_page('Подписка продлена', f'Оплата прошла, спасибо!{end_text}', status_code=200, ok=True)

    if status_value in (GuestPurchaseStatus.FAILED.value, GuestPurchaseStatus.EXPIRED.value):
        return _message_page(
            'Не удалось завершить продление',
            f'Если деньги списаны, обратитесь в поддержку и назовите номер платежа {purchase.id}.',
            status_code=200,
        )

    # PENDING (ждём шлюз) или PAID (шлюз подтвердил, продляем): обновляемся, пока покупка свежая.
    created_at = purchase.created_at
    fresh = created_at is None or datetime.now(UTC) - created_at < _STATUS_REFRESH_WINDOW
    if status_value == GuestPurchaseStatus.PAID.value:
        title, text = 'Платёж получен', 'Продлеваем подписку, это займёт несколько секунд.'
    else:
        title, text = 'Ждём подтверждение оплаты', 'Страница обновится сама, как только платёж подтвердится.'
    return _html_response(
        title,
        f'<h1>{html.escape(title)}</h1><p style="margin-top:10px">{html.escape(text)}</p>',
        refresh_seconds=5 if fresh else None,
    )


@router.get('/renew/{short_uuid}', summary='Страница продления подписки по ссылке')
async def renew_page(
    short_uuid: str,
    request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
) -> HTMLResponse:
    """Форма выбора периода и способа оплаты. Ничего не меняет (GET дёргают сканеры ссылок)."""
    if not _SHORT_UUID_RE.fullmatch(short_uuid):
        return _not_available_page()
    if await _is_rate_limited(request, short_uuid, 'public_renew_view', ip_limit=60, sub_limit=30, window=60):
        return _rate_limited_page()

    subscription = await get_subscription_by_remnawave_short_uuid(db, short_uuid)
    upsell = _should_upsell(subscription)
    reason = _renewal_block_reason(subscription, for_upsell=upsell)
    if reason is not None:
        logger.info('Public renew page: not available', reason=reason, upsell=upsell)
        return _not_available_page()

    landing = await get_active_landing_by_slug(db, RENEW_LANDING_SLUG)
    payment_options = _payment_options(landing.payment_methods) if landing is not None else []

    upsell_tariff = None
    if upsell:
        upsell_tariff = await get_tariff_by_id(db, RENEW_UPSELL_TARIFF_ID)
        if upsell_tariff is None or not upsell_tariff.is_active:
            logger.error('Public renew page: upsell tariff missing or inactive', tariff_id=RENEW_UPSELL_TARIFF_ID)
            return _message_page(
                'Оформление временно недоступно',
                'Сейчас оформить подписку по ссылке нельзя. Попробуйте позже или обратитесь в поддержку.',
                status_code=503,
            )
        periods = await _upsell_period_options(upsell_tariff, subscription.user)
    else:
        periods = await _period_options(db, subscription)

    if not payment_options or not periods:
        logger.warning(
            'Public renew page: nothing to offer',
            subscription_id=subscription.id,
            upsell=upsell,
            has_landing=landing is not None,
            payment_options=len(payment_options),
            periods=len(periods),
        )
        return _message_page(
            'Оплата временно недоступна',
            'Сейчас продлить подписку по ссылке нельзя. Попробуйте позже или обратитесь в поддержку.',
            status_code=503,
        )

    if upsell:
        heading = html.escape(_strip_lead_emoji(upsell_tariff.name))
        highlighted = upsell_tariff.highlight_period_days
    else:
        tariff = subscription.tariff if subscription.tariff_id else None
        heading = html.escape(_strip_lead_emoji(tariff.name)) if tariff is not None else 'Продление подписки'
        highlighted = tariff.highlight_period_days if tariff is not None else None
    period_values = [p for p, _ in periods]
    if RENEW_DEFAULT_PERIOD_DAYS in period_values:
        selected_period = RENEW_DEFAULT_PERIOD_DAYS  # по умолчанию — 3 месяца
    else:
        selected_period = next((p for p, _ in periods if p == highlighted), periods[0][0])

    savings = _period_savings(periods)
    period_parts: list[str] = []
    for period, pricing in periods:
        checked = ' checked' if period == selected_period else ''
        save = savings.get(period, 0)
        save_html = f'<span class="save">выгода {save}%</span>' if save > 0 else ''
        period_parts.append(
            f'<label class="opt"><input type="radio" name="period" value="{period}" required{checked}>'
            f'<span class="label"><span>{html.escape(_period_label(period))}{_hit_badge(period)}</span>{save_html}</span>'
            f'<span class="price">{_price_html(pricing)}</span></label>'
        )
    period_rows = ''.join(period_parts)

    method_parts: list[str] = []
    for i, (value, label, kind) in enumerate(payment_options):
        checked = ' checked' if i == 0 else ''
        icon = _PM_ICONS.get(kind) or _PM_ICONS['other']
        method_parts.append(
            f'<label class="opt"><input type="radio" name="method" value="{html.escape(value, quote=True)}" required{checked}>'
            f'<span class="pm-icon">{icon}</span><span>{html.escape(label)}</span></label>'
        )
    method_rows = ''.join(method_parts)
    app = _app_source(request.query_params.get('app'))
    app_hidden = f'<input type="hidden" name="app" value="{app}">' if app else ''

    # Относительный action: страница живёт на .../renew/{short_uuid}, форма уходит на .../renew/{short_uuid}/pay
    # и работает при любом префиксе прокси (/api, /cabinet).
    inner = f"""<h1>{heading}</h1>
    <p class="muted" style="margin-top:4px">{html.escape(_expiry_text(subscription))}</p>
    {_discount_banner(periods)}
    <form method="post" action="{html.escape(short_uuid, quote=True)}/pay">
      {app_hidden}
      <h2>Период продления</h2>
      {period_rows}
      <h2>Способ оплаты</h2>
      {method_rows}
      <button type="submit">Перейти к оплате</button>
    </form>"""
    return _html_response('Продление подписки', inner)


@router.post('/renew/{short_uuid}/pay', summary='Создать платёж за продление и перейти к оплате')
async def renew_pay(
    short_uuid: str,
    request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Единственное место, где создаётся покупка и платёж. Цена пересчитывается на сервере."""
    if not _SHORT_UUID_RE.fullmatch(short_uuid):
        return _not_available_page()
    if await _is_rate_limited(request, short_uuid, 'public_renew_pay', ip_limit=10, sub_limit=10, window=600):
        return _rate_limited_page()

    # Читаем urlencoded-тело вручную: без зависимости от python-multipart и с жёстким лимитом размера.
    raw_body = await request.body()
    if len(raw_body) > _MAX_FORM_BYTES:
        return _message_page('Ошибка', 'Некорректный запрос.', status_code=400)
    form = parse_qs(raw_body.decode('utf-8', errors='replace'), keep_blank_values=True)
    period_raw = (form.get('period') or [''])[0].strip()
    method = (form.get('method') or [''])[0].strip()
    app = _app_source((form.get('app') or [''])[0])
    if not period_raw.isascii() or not period_raw.isdigit() or len(period_raw) > 4 or not _METHOD_RE.fullmatch(method):
        return _message_page('Ошибка', 'Выберите период и способ оплаты.', status_code=400)
    period_days = int(period_raw)

    subscription = await get_subscription_by_remnawave_short_uuid(db, short_uuid)
    upsell = _should_upsell(subscription)
    reason = _renewal_block_reason(subscription, for_upsell=upsell)
    if reason is not None:
        logger.info('Public renew pay: not available', reason=reason, upsell=upsell)
        return _not_available_page()

    landing = await get_active_landing_by_slug(db, RENEW_LANDING_SLUG)
    if landing is None:
        logger.error('Public renew pay: landing config is missing', slug=RENEW_LANDING_SLUG)
        return _message_page('Оплата временно недоступна', 'Попробуйте позже.', status_code=503)

    # Цена — только серверная; promo-группа + разовое предложение владельца подписки, как в боте.
    # Апселл: период и цена берутся у целевого платного тарифа, на который переводится подписка.
    renew_target_tariff_id: int | None = None
    if upsell:
        upsell_tariff = await get_tariff_by_id(db, RENEW_UPSELL_TARIFF_ID)
        if upsell_tariff is None or not upsell_tariff.is_active:
            logger.error('Public renew pay: upsell tariff missing or inactive', tariff_id=RENEW_UPSELL_TARIFF_ID)
            return _message_page('Оформление временно недоступно', 'Попробуйте позже.', status_code=503)
        if period_days not in _upsell_periods(upsell_tariff):
            return _message_page('Ошибка', 'Выбранный период недоступен.', status_code=400)
        pricing = await _upsell_price_period(upsell_tariff, period_days, subscription.user)
        if pricing is None:
            return _message_page('Ошибка', 'Выбранный период недоступен.', status_code=400)
        purchase_tariff = upsell_tariff
        tariff_name = upsell_tariff.name
        renew_target_tariff_id = upsell_tariff.id
    else:
        if period_days not in _renewal_periods(subscription):
            return _message_page('Ошибка', 'Выбранный период недоступен.', status_code=400)
        pricing = await _price_period(db, subscription, period_days)
        if pricing is None:
            return _message_page('Ошибка', 'Выбранный период недоступен.', status_code=400)
        purchase_tariff = subscription.tariff if subscription.tariff_id else None
        tariff_name = purchase_tariff.name if purchase_tariff is not None else 'Продление подписки'

    amount_kopeks = pricing.final_total

    try:
        method_config = resolve_landing_payment_method(landing.payment_methods, method, exclude_telegram_only=True)
        validate_payment_amount_limits(method_config, amount_kopeks)
    except GuestPurchaseError as exc:
        return _message_page('Способ оплаты недоступен', exc.message, status_code=exc.status_code)

    subscription_id = subscription.id

    # Покупка создаётся без commit — коммитим, только когда шлюз вернул ссылку на оплату.
    # contact_* — не персональные данные: в записи нет ни email, ни Telegram ID владельца.
    purchase = await create_purchase(
        db,
        landing=landing,
        tariff=purchase_tariff,
        period_days=period_days,
        amount_kopeks=amount_kopeks,
        contact_type='renew',
        contact_value=f'subscription:{subscription_id}',
        payment_method=method,
        source='renew',
        campaign_slug=(f'app_{app}' if app else None),
        renew_subscription_id=subscription_id,
        renew_target_tariff_id=renew_target_tariff_id,
        commit=False,
    )
    purchase_id = purchase.id
    purchase_token = purchase.token

    # return_url собираем сами: пер-метод override лендинга для продления не используется.
    return_url = f'{_public_base_url(request)}/status/{purchase_token}'

    try:
        payment_result = await PaymentService().create_guest_payment(
            db=db,
            amount_kopeks=amount_kopeks,
            payment_method=method,
            description=f'{tariff_name} — {period_days}d',
            purchase_token=purchase_token,
            return_url=return_url,
        )
    except Exception:
        await db.rollback()
        logger.exception('Public renew pay: payment creation crashed', purchase_id=purchase_id, method=method)
        return _message_page(
            'Платёжная система недоступна',
            'Не удалось создать платёж. Попробуйте позже или выберите другой способ оплаты.',
            status_code=502,
        )

    if payment_result is None:
        await db.rollback()
        return _message_page(
            'Платёжная система недоступна',
            'Не удалось создать платёж. Попробуйте позже или выберите другой способ оплаты.',
            status_code=502,
        )

    payment_url = payment_result.get('payment_url')
    if not payment_url or not str(payment_url).startswith(('https://', 'http://')):
        await db.rollback()
        logger.error(
            'Public renew pay: payment created but payment_url is missing or invalid',
            purchase_id=purchase_id,
            provider=payment_result.get('provider'),
        )
        return _message_page(
            'Платёжная система недоступна',
            'Платёжная система вернула некорректный ответ. Попробуйте позже.',
            status_code=502,
        )

    await db.commit()
    logger.info(
        'Public renew payment created',
        purchase_id=purchase_id,
        subscription_id=subscription_id,
        period_days=period_days,
        amount_kopeks=amount_kopeks,
        provider=payment_result.get('provider'),
    )
    # 303: браузер перейдёт на оплату обычным GET, форма повторно не отправится.
    return RedirectResponse(str(payment_url), status_code=303)
