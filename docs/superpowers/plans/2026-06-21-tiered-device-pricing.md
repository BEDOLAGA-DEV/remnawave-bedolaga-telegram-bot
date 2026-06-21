# Tiered Per-Device-Count Pricing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let admins set non-linear per-device-count pricing on tariffs (bulk discount), with users picking any device count `1..max` priced by linear interpolation between admin-defined tiers — fully backward-compatible with the existing flat `device_price_kopeks`.

**Architecture:** Add one JSON column `Tariff.device_price_tiers` (`{total_count: extra_kopeks_per_month}` over base). A single model method `Tariff.get_device_extra_price_per_month(total_count)` is the ONLY place the interpolation/extrapolation/fallback math lives; every pricing site (engine, bot handlers, keyboards, miniapp) calls it. Empty tiers → existing linear path unchanged.

**Tech Stack:** Python 3.12, SQLAlchemy, Alembic, aiogram (bot), FastAPI (cabinet/miniapp), React+TS (bedolaga-cabinet), pytest.

**Spec:** [docs/superpowers/specs/2026-06-21-tiered-device-pricing-design.md](../specs/2026-06-21-tiered-device-pricing-design.md)

**Branch:** `feat/tiered-device-pricing`

---

## Pricing model (reference for all tasks)

- `device_price_tiers` = `{"3": 4000, "5": 7000}` — key = **total** device count (str), value = **extra kopeks/month over base**.
- Implicit anchor `(device_limit, 0)`. Base devices (`<= device_limit`) are free.
- `extra(N)` = linear interpolation between surrounding anchors; above the top anchor, extrapolate by the last segment's slope (monotone). Empty tiers → `(N - device_limit) * device_price_kopeks`.
- Result is **per month**. Period total = `extra(N) * months`. Mid-period add-on = `(extra(new) - extra(current)) * min(days_left, 30) / 30`.

Target grid (`device_limit=1`, base 30 ₽, tiers `{"3":4000,"5":7000}`):

| N | extra/mo | total 30d |
|---|----------|-----------|
| 1 | 0 | 30 ₽ |
| 2 | 2000 | 50 ₽ |
| 3 | 4000 | 70 ₽ |
| 4 | 5500 | 85 ₽ |
| 5 | 7000 | 100 ₽ |
| 7 | 10000 | 130 ₽ |

---

## Task 1: Model field + interpolation helper

**Files:**
- Modify: `app/database/models.py` (Tariff class, after `wl_traffic_topup_packages` ~line 1769; new method near `get_traffic_topup_packages` ~line 1865)
- Test: `tests/database/test_device_price_tiers.py`

- [ ] **Step 1: Write the failing test**

Create `tests/database/test_device_price_tiers.py`:

```python
from app.database.models import Tariff


def _tariff(device_limit=1, device_price_kopeks=None, tiers=None):
    t = Tariff(name='t', device_limit=device_limit)
    t.device_price_kopeks = device_price_kopeks
    t.device_price_tiers = tiers if tiers is not None else {}
    return t


def test_tiers_target_grid():
    t = _tariff(tiers={'3': 4000, '5': 7000})
    assert t.get_device_extra_price_per_month(1) == 0
    assert t.get_device_extra_price_per_month(2) == 2000
    assert t.get_device_extra_price_per_month(3) == 4000
    assert t.get_device_extra_price_per_month(4) == 5500
    assert t.get_device_extra_price_per_month(5) == 7000


def test_tiers_extrapolate_above_top_anchor():
    t = _tariff(tiers={'3': 4000, '5': 7000})
    # slope of last segment = (7000-4000)/(5-3) = 1500 -> 7000 + 1500*2
    assert t.get_device_extra_price_per_month(7) == 10000


def test_base_devices_free():
    t = _tariff(device_limit=2, tiers={'5': 7000})
    assert t.get_device_extra_price_per_month(1) == 0
    assert t.get_device_extra_price_per_month(2) == 0


def test_single_anchor_interpolates_from_base():
    t = _tariff(device_limit=1, tiers={'5': 8000})
    # anchors (1,0)-(5,8000), slope 2000/dev -> N=3 => 4000
    assert t.get_device_extra_price_per_month(3) == 4000


def test_linear_fallback_when_no_tiers():
    t = _tariff(device_limit=1, device_price_kopeks=5000, tiers={})
    assert t.get_device_extra_price_per_month(3) == 10000


def test_empty_everything_is_free():
    t = _tariff(device_limit=1, device_price_kopeks=None, tiers={})
    assert t.get_device_extra_price_per_month(3) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/database/test_device_price_tiers.py -v`
Expected: FAIL — `AttributeError: 'Tariff' object has no attribute 'device_price_tiers'` (and no `get_device_extra_price_per_month`).

- [ ] **Step 3: Add the column**

In `app/database/models.py`, immediately after the `wl_traffic_topup_packages` column (~line 1769), add:

```python
    # Тарифные тиры устройств: JSON {"3": 4000, "5": 7000} — total_count: extra_kopeks/мес
    # сверх базы. Пусто = использовать линейный device_price_kopeks (старое поведение).
    device_price_tiers = Column(JSON, default=dict)
```

- [ ] **Step 4: Add the helper method**

In `app/database/models.py`, next to `get_traffic_topup_packages` (~line 1865), add:

```python
    def get_device_extra_price_per_month(self, total_count: int) -> int:
        """Доплата коп/мес сверх базы за total_count устройств.

        Источник — device_price_tiers (интерполяция между якорями, экстраполяция
        по наклону последнего сегмента выше верхнего якоря). Пустые тиры —
        линейный fallback на device_price_kopeks. Базовые device_limit бесплатны.
        """
        base = self.device_limit or 0
        if (total_count or 0) <= base:
            return 0

        tiers = self.device_price_tiers or {}
        if not tiers:
            unit = self.device_price_kopeks or 0
            return max(0, ((total_count or 0) - base) * unit)

        anchors = {base: 0}
        for count_str, price in tiers.items():
            try:
                anchors[int(count_str)] = int(price)
            except (TypeError, ValueError):
                continue
        points = sorted(anchors.items())

        n = total_count
        for count, price in points:
            if count == n:
                return max(0, price)

        for i in range(len(points) - 1):
            a_count, a_price = points[i]
            b_count, b_price = points[i + 1]
            if a_count < n < b_count:
                slope = (b_price - a_price) / (b_count - a_count)
                return max(0, round(a_price + slope * (n - a_count)))

        # n выше верхнего якоря — экстраполяция по наклону последнего сегмента
        a_count, a_price = points[-2] if len(points) >= 2 else (base, 0)
        b_count, b_price = points[-1]
        denom = (b_count - a_count) or 1
        slope = (b_price - a_price) / denom
        return max(0, round(b_price + slope * (n - b_count)))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/database/test_device_price_tiers.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add app/database/models.py tests/database/test_device_price_tiers.py
git commit -m "feat(tariff): device_price_tiers field + interpolation helper"
```

---

## Task 2: Migration 0117

**Files:**
- Create: `migrations/alembic/versions/0117_add_device_price_tiers_to_tariffs.py`

- [ ] **Step 1: Create the migration**

Create the file verbatim (mirrors `0063_add_wl_tariff_traffic_fields.py`, idempotent):

```python
"""add device_price_tiers to tariffs

Adds per-tariff non-linear device pricing:
  - device_price_tiers: JSON {"3": 4000, "5": 7000} (total_device_count: extra_kopeks_per_month
    over base). Empty = use linear device_price_kopeks (legacy behaviour).

Revision ID: 0117
Revises: 0116
Create Date: 2026-06-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0117'
down_revision: Union[str, None] = '0116'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: skip column if it already exists from a custom branch.
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = {c['name'] for c in inspector.get_columns('tariffs')}

    if 'device_price_tiers' not in existing:
        op.add_column(
            'tariffs',
            sa.Column('device_price_tiers', sa.JSON(), nullable=True, server_default='{}'),
        )

    op.execute("UPDATE tariffs SET device_price_tiers = '{}' WHERE device_price_tiers IS NULL")


def downgrade() -> None:
    op.drop_column('tariffs', 'device_price_tiers')
```

- [ ] **Step 2: Apply the migration**

Run: `python -m alembic upgrade head`
Expected: runs `0117`, no errors. (If alembic invoked differently in this repo, use the project's standard, e.g. `alembic upgrade head`.)

- [ ] **Step 3: Verify current revision**

Run: `python -m alembic current`
Expected: shows `0117 (head)`.

- [ ] **Step 4: Commit**

```bash
git add migrations/alembic/versions/0117_add_device_price_tiers_to_tariffs.py
git commit -m "feat(db): migration 0117 add device_price_tiers"
```

---

## Task 3: CRUD create/update params

**Files:**
- Modify: `app/database/crud/tariff.py` (`create_tariff` ~164-269, `update_tariff` ~272-409)

- [ ] **Step 1: Add param to `create_tariff`**

In the signature, after `max_device_limit: int | None = None,` (~line 174) add:

```python
    device_price_tiers: dict[str, int] | None = None,
```

In the `Tariff(...)` constructor, after `max_device_limit=max_device_limit,` (~line 215) add:

```python
        device_price_tiers=device_price_tiers or {},
```

- [ ] **Step 2: Add param to `update_tariff`**

In the signature, after `wl_traffic_topup_packages: dict[str, int] | None = None,` (~line 316) add:

```python
    device_price_tiers: dict[str, int] | None = None,
```

In the body, after the `wl_traffic_topup_packages` update block (~lines 392-393) add:

```python
    if device_price_tiers is not None:
        tariff.device_price_tiers = device_price_tiers
```

- [ ] **Step 3: Verify import**

Run: `python -c "import app.database.crud.tariff"`
Expected: no error.

- [ ] **Step 4: Commit**

```bash
git add app/database/crud/tariff.py
git commit -m "feat(crud): device_price_tiers param in create/update_tariff"
```

---

## Task 4: Pricing engine uses the helper

**Files:**
- Modify: `app/services/pricing_engine.py` (`_calculate_tariff_core` device block ~587-596)
- Test: `tests/services/test_pricing_engine_device_tiers.py`

- [ ] **Step 1: Replace the flat device formula**

Current (lines ~587-596):

```python
        # --- Extra devices (monthly × months) ---
        device_price_per_unit = (
            tariff.device_price_kopeks if tariff.device_price_kopeks is not None else settings.PRICE_PER_DEVICE
        )
        tariff_device_limit = tariff.device_limit or 0
        extra_devices = max(0, (device_limit or 0) - tariff_device_limit)
        if is_daily and period_days <= 1:
            devices_price = extra_devices * device_price_per_unit
        else:
            devices_price = extra_devices * device_price_per_unit * months
```

Replace with:

```python
        # --- Extra devices (monthly × months) ---
        # Tiered pricing via Tariff.get_device_extra_price_per_month (handles tiers
        # with interpolation, else linear device_price_kopeks fallback).
        tariff_device_limit = tariff.device_limit or 0
        extra_devices = max(0, (device_limit or 0) - tariff_device_limit)
        if tariff.device_price_tiers or tariff.device_price_kopeks is not None:
            devices_price_per_month = tariff.get_device_extra_price_per_month(device_limit or 0)
        else:
            # No tariff device pricing configured — fall back to global flat rate.
            devices_price_per_month = extra_devices * settings.PRICE_PER_DEVICE
        if is_daily and period_days <= 1:
            devices_price = devices_price_per_month
        else:
            devices_price = devices_price_per_month * months
```

- [ ] **Step 2: Write the test**

Create `tests/services/test_pricing_engine_device_tiers.py`:

```python
from app.database.models import Tariff


def _tariff():
    t = Tariff(name='t', device_limit=1)
    t.device_price_kopeks = None
    t.device_price_tiers = {'3': 4000, '5': 7000}
    return t


def test_engine_devices_per_month_matches_helper():
    # The engine prices devices via the helper, not a flat multiply.
    t = _tariff()
    assert t.get_device_extra_price_per_month(5) == 7000
    assert t.get_device_extra_price_per_month(3) == 4000
```

(Engine entry points are async + DB-bound; this pins the helper contract the engine now depends on. Wired-engine check is Step 4.)

- [ ] **Step 3: Run test**

Run: `python -m pytest tests/services/test_pricing_engine_device_tiers.py -v`
Expected: PASS.

- [ ] **Step 4: Verify engine import + no regressions**

Run: `python -c "import app.services.pricing_engine"`
Expected: no error.
Run: `python -m pytest tests/cabinet/subscription/test_traffic_pricing.py -q`
Expected: existing pricing tests still PASS (empty tiers → unchanged behaviour).

- [ ] **Step 5: Commit**

```bash
git add app/services/pricing_engine.py tests/services/test_pricing_engine_device_tiers.py
git commit -m "feat(pricing): tariff device price via tier helper"
```

---

## Task 5: Bot device add/change handlers

**Files:**
- Modify: `app/handlers/subscription/devices.py` (gate ~187/281/546/1422; pricing ~334-455, ~574-607, ~1450-1525)

- [ ] **Step 1: Add a tariff-devices-enabled helper**

Near the top of `app/handlers/subscription/devices.py` (after `_get_remnawave_uuid`), add:

```python
def _tariff_devices_enabled(tariff) -> bool:
    """Докупка устройств доступна, если у тарифа есть тиры или цена за устройство."""
    if tariff is None:
        return False
    if getattr(tariff, 'device_price_tiers', None):
        return True
    price = getattr(tariff, 'device_price_kopeks', None)
    return price is not None and price > 0
```

- [ ] **Step 2: Update the four gates**

In `handle_change_devices` (~187), `confirm_change_devices` (~281), `execute_change_devices` (~546), `confirm_add_devices` (~1422) — each currently does:

```python
        if tariff_device_price is None or tariff_device_price <= 0:
            await callback.answer(
                texts.t('TARIFF_DEVICES_DISABLED', ...),
                show_alert=True,
            )
            return
        price_per_device = tariff_device_price
```

Change the condition to use the helper (keep each site's existing `texts.t(...)` message text unchanged):

```python
        if not _tariff_devices_enabled(tariff):
            await callback.answer(
                texts.t('TARIFF_DEVICES_DISABLED', ...),
                show_alert=True,
            )
            return
        price_per_device = tariff_device_price or 0
```

- [ ] **Step 3: Helper-based pricing — `confirm_change_devices`**

In the `if devices_difference > 0:` block (~336-353), replace the tariff branch's `chargeable_devices` + `devices_price_per_month = chargeable_devices * price_per_device` with:

```python
        if tariff:
            devices_price_per_month = max(
                0,
                tariff.get_device_extra_price_per_month(new_devices_count)
                - tariff.get_device_extra_price_per_month(current_devices),
            )
        else:
            if current_devices < settings.DEFAULT_DEVICE_LIMIT:
                free_devices = settings.DEFAULT_DEVICE_LIMIT - current_devices
                chargeable_devices = max(0, additional_devices - free_devices)
            else:
                chargeable_devices = additional_devices
            devices_price_per_month = chargeable_devices * price_per_device
```

(Trailing prorate `effective_days`, discount, `price = int(discounted_per_month * effective_days / 30)`, `max(100, price)` stay unchanged.)

- [ ] **Step 4: Helper-based pricing — `execute_change_devices`**

In the recompute-under-lock block (~575-590), replace the tariff branch:

```python
    devices_difference = new_devices_count - current_devices
    if devices_difference > 0:
        if tariff:
            devices_price_per_month = max(
                0,
                tariff.get_device_extra_price_per_month(new_devices_count)
                - tariff.get_device_extra_price_per_month(current_devices),
            )
        elif current_devices < settings.DEFAULT_DEVICE_LIMIT:
            free_devices = settings.DEFAULT_DEVICE_LIMIT - current_devices
            chargeable_devices = max(0, devices_difference - free_devices)
            devices_price_per_month = chargeable_devices * price_per_device
        else:
            chargeable_devices = devices_difference
            devices_price_per_month = chargeable_devices * price_per_device
```

(Keep the trailing prorate/discount/`max(100, price)` lines.)

- [ ] **Step 5: Helper-based pricing — `confirm_add_devices`**

In `confirm_add_devices` (~1450-1465), `new_total_devices = subscription.device_limit + devices_count` is already computed above. Replace the `chargeable_devices`/`devices_price_per_month` derivation:

```python
    current_devices = subscription.device_limit or 1
    if tariff:
        devices_price_per_month = max(
            0,
            tariff.get_device_extra_price_per_month(new_total_devices)
            - tariff.get_device_extra_price_per_month(current_devices),
        )
    elif current_devices < settings.DEFAULT_DEVICE_LIMIT:
        free_devices = settings.DEFAULT_DEVICE_LIMIT - current_devices
        chargeable_devices = max(0, devices_count - free_devices)
        devices_price_per_month = chargeable_devices * price_per_device
    else:
        chargeable_devices = devices_count
        devices_price_per_month = chargeable_devices * price_per_device
```

In both daily/non-daily prorate branches below, the `if chargeable_devices > 0:` floor guard must become `if devices_price_per_month > 0:` so the `max(100, price)` floor still applies for tiered prices.

- [ ] **Step 6: Verify import**

Run: `python -c "import app.handlers.subscription.devices"`
Expected: no error.

- [ ] **Step 7: Manual verification**

Per the run skill: start the bot, create a tariff with `device_price_tiers={"3":4000,"5":7000}`, `device_price_kopeks=null`, `device_limit=1`, buy it, open "Изменение устройств", confirm 3 → +40 ₽, 5 → +70 ₽, 4 → +55 ₽ (full remaining month).

- [ ] **Step 8: Commit**

```bash
git add app/handlers/subscription/devices.py
git commit -m "feat(bot): tiered device pricing in add/change handlers"
```

---

## Task 6: Bot keyboard per-button price

**Files:**
- Modify: `app/keyboards/inline.py` (`get_change_devices_keyboard` ~2916-3021)

- [ ] **Step 1: Use the helper for per-button price**

In the `elif devices_count > current_devices:` branch (~2978-2996), replace the `chargeable_devices` + `price_per_month` derivation:

```python
            if tariff is not None:
                price_per_month = max(
                    0,
                    tariff.get_device_extra_price_per_month(devices_count)
                    - tariff.get_device_extra_price_per_month(current_devices),
                )
            else:
                current_chargeable = max(0, current_devices - default_device_limit)
                new_chargeable = max(0, devices_count - default_device_limit)
                chargeable_devices = new_chargeable - current_chargeable
                price_per_month = chargeable_devices * device_price_per_month

            if price_per_month > 0:
                discounted_per_month, discount_per_month = apply_percentage_discount(
                    price_per_month,
                    discount_percent,
                )
                total_price = int(discounted_per_month * price_multiplier)
                total_price = max(100, total_price)
                price_text = f' (+{total_price // 100}₽{period_text})'
                total_discount = int(discount_per_month * price_multiplier)
                if discount_percent > 0 and total_discount > 0:
                    price_text += f' (скидка {discount_percent}%: -{total_discount // 100}₽)'
                action_text = ''
            else:
                price_text = ' (бесплатно)'
                action_text = ''
```

- [ ] **Step 2: Verify import**

Run: `python -c "import app.keyboards.inline"`
Expected: no error.

- [ ] **Step 3: Commit**

```bash
git add app/keyboards/inline.py
git commit -m "feat(bot): device keyboard prices via tier helper"
```

---

## Task 7: Bot admin — edit device tiers

**Files:**
- Modify: `app/handlers/admin/tariffs.py` (helpers ~36-87; `format_tariff_info` ~312-324; edit flow ~1455-1539)
- Test: `tests/handlers/test_admin_device_tiers_parse.py`

- [ ] **Step 1: Write the failing parser test**

Create `tests/handlers/test_admin_device_tiers_parse.py`:

```python
from app.handlers.admin.tariffs import _parse_device_price_tiers


def test_parse_basic():
    assert _parse_device_price_tiers('3:4000, 5:7000') == {'3': 4000, '5': 7000}


def test_parse_separators_and_base_excluded():
    # base count (1) ignored (must be >= 2); ';' and '=' accepted
    assert _parse_device_price_tiers('1:0; 3=4000') == {'3': 4000}


def test_parse_empty_and_garbage():
    assert _parse_device_price_tiers('') == {}
    assert _parse_device_price_tiers('abc, 3:x') == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/handlers/test_admin_device_tiers_parse.py -v`
Expected: FAIL — `ImportError: cannot import name '_parse_device_price_tiers'`.

- [ ] **Step 3: Add parse/format helpers**

In `app/handlers/admin/tariffs.py`, after `_format_period_prices_for_edit` (~line 87), add:

```python
def _parse_device_price_tiers(text: str) -> dict[str, int]:
    """Парсит тиры устройств. Формат: "3:4000, 5:7000" (кол-во:доплата_коп/мес).

    Кол-во должно быть >= 2 (1 устройство — база, доплата 0). Цена >= 0.
    """
    tiers: dict[str, int] = {}
    text = text.replace(';', ',').replace('=', ':')

    for part in text.split(','):
        part = part.strip()
        if not part or ':' not in part:
            continue
        count_str, price_str = part.split(':', 1)
        try:
            count = int(count_str.strip())
            price = int(price_str.strip())
            if count >= 2 and price >= 0:
                tiers[str(count)] = price
        except ValueError:
            continue

    return tiers


def _format_device_price_tiers_display(tiers: dict[str, int]) -> str:
    """Форматирует тиры устройств для отображения."""
    if not tiers:
        return 'Не заданы'

    lines = []
    for count_str in sorted(tiers.keys(), key=int):
        lines.append(f'  • {count_str} устр.: +{format_price_kopeks(tiers[count_str])}/мес')

    return '\n'.join(lines)


def _format_device_price_tiers_for_edit(tiers: dict[str, int]) -> str:
    """Форматирует тиры устройств для редактирования."""
    if not tiers:
        return '3:4000, 5:7000'

    return ', '.join(f'{c}:{tiers[c]}' for c in sorted(tiers.keys(), key=int))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/handlers/test_admin_device_tiers_parse.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Show tiers in `format_tariff_info`**

In `format_tariff_info` device block (~312-318), after computing `device_price_display`, add:

```python
    device_tiers = getattr(tariff, 'device_price_tiers', None) or {}
    if device_tiers:
        device_price_display = 'Тиры:\n' + _format_device_price_tiers_display(device_tiers)
```

- [ ] **Step 6: Extend the edit flow to accept tiers**

In `start_edit_tariff_device_price` (~1455-1493), replace the `current_price` derivation + `edit_text(...)` prompt:

```python
    device_tiers = getattr(tariff, 'device_price_tiers', None) or {}
    if device_tiers:
        current_price = 'Тиры: ' + _format_device_price_tiers_for_edit(device_tiers)
    elif device_price is not None and device_price > 0:
        current_price = format_price_kopeks(device_price) + '/мес (линейно)'
    else:
        current_price = 'Недоступно (докупка устройств запрещена)'

    await callback.message.edit_text(
        f'📱💰 <b>Редактирование цены за устройство</b>\n\n'
        f'Текущее: <b>{current_price}</b>\n\n'
        'Введите одно из:\n'
        '• <b>Тиры</b> (нелинейно): <code>3:4000, 5:7000</code> '
        '(кол-во:доплата_коп/мес сверх базы)\n'
        '• <b>Линейно</b>: одно число — цена коп/мес за устройство\n'
        '• <code>0</code> или <code>-</code> — докупка недоступна',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_view:{tariff_id}')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()
```

In `process_edit_tariff_device_price` (~1497-1539), replace the parse + update logic (keep the trailing `subs_count`/success `message.answer(...)` block unchanged):

```python
    text = message.text.strip()

    if text == '-' or text == '0':
        tariff = await update_tariff(db, tariff, device_price_kopeks=None, device_price_tiers={})
    elif ':' in text or ',' in text:
        tiers = _parse_device_price_tiers(text)
        if not tiers:
            await message.answer(
                'Не удалось разобрать тиры. Формат: <code>3:4000, 5:7000</code>',
                parse_mode='HTML',
            )
            return
        tariff = await update_tariff(db, tariff, device_price_tiers=tiers)
    else:
        try:
            device_price = int(text)
            if device_price < 0:
                raise ValueError
        except ValueError:
            await message.answer(
                'Введите число (линейно), тиры (<code>3:4000, 5:7000</code>) '
                'или <code>0</code>/<code>-</code> для отключения.',
                parse_mode='HTML',
            )
            return
        tariff = await update_tariff(db, tariff, device_price_kopeks=device_price, device_price_tiers={})

    await state.clear()
```

- [ ] **Step 7: Verify import**

Run: `python -c "import app.handlers.admin.tariffs"`
Expected: no error.

- [ ] **Step 8: Commit**

```bash
git add app/handlers/admin/tariffs.py tests/handlers/test_admin_device_tiers_parse.py
git commit -m "feat(admin-bot): edit device price tiers"
```

---

## Task 8: Cabinet API schema + routes

**Files:**
- Modify: `app/cabinet/schemas/tariffs.py` (Detail ~91, Create ~156, Update ~201)
- Modify: `app/cabinet/routes/admin_tariffs.py` (get ~247, create ~312, update ~390)
- Test: `tests/cabinet/subscription/test_device_tiers_route.py`

- [ ] **Step 1: Add the schema field (3 places)**

`TariffDetailResponse` (after `max_device_limit` ~line 92):
```python
    device_price_tiers: dict[str, int] = Field(default_factory=dict)
```
`TariffCreateRequest` (after `max_device_limit` ~line 156):
```python
    device_price_tiers: dict[str, int] = Field(default_factory=dict, description='count -> extra kopeks/month')
```
`TariffUpdateRequest` (after `max_device_limit` ~line 201):
```python
    device_price_tiers: dict[str, int] | None = None
```

- [ ] **Step 2: Wire the routes**

`get_tariff` response (after `max_device_limit=tariff.max_device_limit,` ~line 247):
```python
        device_price_tiers=tariff.device_price_tiers or {},
```
`create_new_tariff` call (after `max_device_limit=request.max_device_limit,` ~line 312):
```python
        device_price_tiers=request.device_price_tiers,
```
`update_existing_tariff` (after the `max_device_limit` conditional ~line 393):
```python
    if request.device_price_tiers is not None:
        updates['device_price_tiers'] = request.device_price_tiers
```

- [ ] **Step 3: Write the round-trip test**

Create `tests/cabinet/subscription/test_device_tiers_route.py`:

```python
from app.cabinet.schemas.tariffs import TariffCreateRequest, TariffUpdateRequest


def test_create_request_accepts_tiers():
    req = TariffCreateRequest(name='t', device_price_tiers={'3': 4000, '5': 7000})
    assert req.device_price_tiers == {'3': 4000, '5': 7000}


def test_create_request_defaults_empty():
    req = TariffCreateRequest(name='t')
    assert req.device_price_tiers == {}


def test_update_request_optional():
    req = TariffUpdateRequest()
    assert req.device_price_tiers is None
    req2 = TariffUpdateRequest(device_price_tiers={'2': 2000})
    assert req2.device_price_tiers == {'2': 2000}
```

- [ ] **Step 4: Run test**

Run: `python -m pytest tests/cabinet/subscription/test_device_tiers_route.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Verify route import**

Run: `python -c "import app.cabinet.routes.admin_tariffs"`
Expected: no error.

- [ ] **Step 6: Commit**

```bash
git add app/cabinet/schemas/tariffs.py app/cabinet/routes/admin_tariffs.py tests/cabinet/subscription/test_device_tiers_route.py
git commit -m "feat(cabinet-api): device_price_tiers in tariff schema + routes"
```

---

## Task 9: MiniApp device pricing

**Files:**
- Modify: `app/webapi/routes/miniapp.py` (options list ~5020-5048; change endpoint ~6072-6125)

- [ ] **Step 1: Options-list site (~5020-5048)**

Replace the resolution + availability (~5021-5029):
```python
    tariff_has_devices = bool(tariff) and (
        bool(getattr(tariff, 'device_price_tiers', None))
        or (tariff.device_price_kopeks is not None and tariff.device_price_kopeks > 0)
    )
    if tariff_has_devices:
        base_device_price = tariff.device_price_kopeks or 0
        max_devices_setting = tariff.max_device_limit
    elif tariff:
        base_device_price = 0
        max_devices_setting = tariff.max_device_limit
    else:
        base_device_price = settings.PRICE_PER_DEVICE
        max_devices_setting = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else None

    devices_can_update = bool(tariff_has_devices) or (not tariff and base_device_price > 0)
```

In the per-option loop (~5042-5047), replace `chargeable * base_device_price`:
```python
    for value in range(1, max_devices + 1):
        if tariff is not None:
            per_month = max(
                0,
                tariff.get_device_extra_price_per_month(value)
                - tariff.get_device_extra_price_per_month(current_device_limit),
            )
        else:
            chargeable = max(0, value - default_device_limit)
            per_month = chargeable * base_device_price
        discounted_per_month, _ = apply_percentage_discount(
            per_month,
            devices_discount,
        )
        devices_options.append(
```

(Leave the rest of the `devices_options.append(...)` body unchanged.)

- [ ] **Step 2: Change-endpoint site (~6072-6125)**

Replace the resolution (~6072-6077):
```python
    tariff_has_devices = bool(tariff) and (
        bool(getattr(tariff, 'device_price_tiers', None))
        or (tariff.device_price_kopeks is not None and tariff.device_price_kopeks > 0)
    )
    if tariff_has_devices:
        tariff_device_price = tariff.device_price_kopeks or 0
        tariff_max_device_limit = tariff.max_device_limit
    elif tariff:
        tariff_device_price = 0
        tariff_max_device_limit = tariff.max_device_limit
    else:
        tariff_device_price = settings.PRICE_PER_DEVICE
        tariff_max_device_limit = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else None
```

Replace the availability gate (~6080):
```python
    # Block purchase if device add-on unavailable for this tariff
    if not (tariff_has_devices or (not tariff and tariff_device_price > 0)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={'code': 'devices_unavailable', 'message': 'Докупка устройств недоступна'},
        )
```

In the charge block (~6120-6125), replace `price_per_month = chargeable_diff * tariff_device_price`:
```python
    if devices_difference > 0:
        if tariff is not None:
            price_per_month = max(
                0,
                tariff.get_device_extra_price_per_month(new_devices)
                - tariff.get_device_extra_price_per_month(current_devices),
            )
        else:
            current_chargeable = max(0, current_devices - settings.DEFAULT_DEVICE_LIMIT)
            new_chargeable = max(0, new_devices - settings.DEFAULT_DEVICE_LIMIT)
            chargeable_diff = new_chargeable - current_chargeable
            price_per_month = chargeable_diff * tariff_device_price
```

(Leave the trailing prorate/discount/charge logic unchanged.)

- [ ] **Step 3: Verify import**

Run: `python -c "import app.webapi.routes.miniapp"`
Expected: no error.

- [ ] **Step 4: Commit**

```bash
git add app/webapi/routes/miniapp.py
git commit -m "feat(miniapp): tiered device pricing in options + change endpoint"
```

---

## Task 10: React cabinet editor + locales

**Files:**
- Modify: `bedolaga-cabinet/src/api/tariffs.ts` (TariffDetail, TariffCreateRequest, TariffUpdateRequest)
- Modify: `bedolaga-cabinet/src/pages/AdminTariffCreate.tsx` (state ~44-61, load ~100-132, submit ~156-177, device card ~766-808)
- Modify: `bedolaga-cabinet/src/locales/{ru,en,zh,fa}.json` (admin.tariffs)

- [ ] **Step 1: API types**

In `bedolaga-cabinet/src/api/tariffs.ts`, add after each `max_device_limit` field:
- `TariffDetail`: `device_price_tiers: Record<string, number>;`
- `TariffCreateRequest`: `device_price_tiers?: Record<string, number>;`
- `TariffUpdateRequest`: `device_price_tiers?: Record<string, number>;`

- [ ] **Step 2: Component state**

In `AdminTariffCreate.tsx`, after the device state (~line 46) add:

```tsx
  const [devicePriceTiers, setDevicePriceTiers] = useState<Record<string, number>>({});
  const [newTierDevices, setNewTierDevices] = useState<number | ''>(2);
  const [newTierPrice, setNewTierPrice] = useState<number | ''>(40);
  const [editingTierPrices, setEditingTierPrices] = useState<Record<string, string>>({});
```

- [ ] **Step 3: Load on edit**

In the `useQuery` select callback (~line 120, after `setMaxDeviceLimit(...)`) add:

```tsx
      setDevicePriceTiers(data.device_price_tiers || {});
```

- [ ] **Step 4: Submit**

In the `handleSubmit` data object (~line 163, after `max_device_limit`) add:

```tsx
      device_price_tiers:
        Object.keys(devicePriceTiers).length > 0 ? devicePriceTiers : {},
```

- [ ] **Step 5: Tier editor UI**

In the device card (`AdminTariffCreate.tsx` ~line 805, before the card's closing `</div>` after the max-device hint), insert (mirrors the traffic-packages editor; keys are device counts):

```tsx
            {/* Device price tiers (non-linear) */}
            <div className="rounded-lg border border-dashed border-dark-600 bg-dark-800/50 p-3">
              <h5 className="mb-2 text-xs font-medium text-dark-400">
                {t('admin.tariffs.devicePriceTiersTitle')}
              </h5>
              <div className="flex flex-wrap items-end gap-2">
                <div>
                  <label className="mb-1 block text-xs text-dark-500">
                    {t('admin.tariffs.deviceCountLabel')}
                  </label>
                  <input
                    type="number"
                    value={newTierDevices}
                    onChange={createNumberInputHandler(setNewTierDevices, 2)}
                    className="input w-20"
                    placeholder="2"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs text-dark-500">
                    {t('admin.tariffs.priceLabel')}
                  </label>
                  <input
                    type="number"
                    value={newTierPrice}
                    onChange={createNumberInputHandler(setNewTierPrice, 1)}
                    className="input w-24"
                    placeholder="40"
                  />
                </div>
                <button
                  type="button"
                  onClick={() => {
                    const count = toNumber(newTierDevices, 0);
                    const price = toNumber(newTierPrice, 0);
                    if (count >= 2 && price >= 0 && !devicePriceTiers[String(count)]) {
                      setDevicePriceTiers((prev) => ({ ...prev, [String(count)]: price * 100 }));
                      setNewTierDevices(2);
                      setNewTierPrice(40);
                    }
                  }}
                  disabled={
                    newTierDevices === '' ||
                    newTierPrice === '' ||
                    !!devicePriceTiers[String(newTierDevices)]
                  }
                  className="btn-primary flex items-center gap-1 px-3 py-2 text-sm"
                >
                  <PlusIcon />
                  {t('admin.tariffs.addButton')}
                </button>
              </div>
            </div>
            <div>
              <span className="text-sm text-dark-400">
                {t('admin.tariffs.devicePriceTiersLabel')}
              </span>
              {Object.keys(devicePriceTiers).length === 0 ? (
                <div className="mt-2 py-4 text-center text-sm text-dark-500">
                  {t('admin.tariffs.noDeviceTiersHint')}
                </div>
              ) : (
                <div className="mt-2 space-y-2">
                  {Object.entries(devicePriceTiers)
                    .sort(([a], [b]) => Number(a) - Number(b))
                    .map(([count, priceKopeks]) => (
                      <div key={count} className="flex items-center gap-2 rounded-lg bg-dark-800 p-2">
                        <span className="w-16 text-sm font-medium text-dark-300">
                          {count} {t('admin.tariffs.devicesTierUnit')}
                        </span>
                        <input
                          type="number"
                          value={
                            editingTierPrices[count] !== undefined
                              ? editingTierPrices[count]
                              : priceKopeks / 100
                          }
                          onChange={(e) => {
                            const val = e.target.value;
                            setEditingTierPrices((prev) => ({ ...prev, [count]: val }));
                            if (val !== '') {
                              const num = parseFloat(val);
                              if (!isNaN(num)) {
                                setDevicePriceTiers((prev) => ({
                                  ...prev,
                                  [count]: Math.max(0, num) * 100,
                                }));
                              }
                            }
                          }}
                          onBlur={(e) => {
                            if (e.target.value === '') {
                              setDevicePriceTiers((prev) => ({ ...prev, [count]: 0 }));
                            }
                            setEditingTierPrices((prev) => {
                              const copy = { ...prev };
                              delete copy[count];
                              return copy;
                            });
                          }}
                          className="input w-24"
                          step={1}
                          placeholder="0"
                        />
                        <span className="text-xs text-dark-400">₽</span>
                        <div className="flex-1" />
                        <button
                          type="button"
                          onClick={() => {
                            setDevicePriceTiers((prev) => {
                              const copy = { ...prev };
                              delete copy[count];
                              return copy;
                            });
                          }}
                          className="rounded-lg p-2 text-dark-400 transition-colors hover:bg-error-500/20 hover:text-error-400"
                        >
                          <TrashIcon className="h-4 w-4" />
                        </button>
                      </div>
                    ))}
                </div>
              )}
            </div>
```

- [ ] **Step 6: Locale keys (all 4 files)**

In each `bedolaga-cabinet/src/locales/{ru,en,zh,fa}.json`, in the `admin.tariffs` object add:

ru.json:
```json
    "devicePriceTiersTitle": "Тиры цены устройств",
    "deviceCountLabel": "Кол-во устройств",
    "devicesTierUnit": "устр.",
    "devicePriceTiersLabel": "Тиры (доплата/мес сверх базы):",
    "noDeviceTiersHint": "Тиры не заданы. Добавьте, чтобы включить нелинейную цену.",
```
en.json:
```json
    "devicePriceTiersTitle": "Device price tiers",
    "deviceCountLabel": "Device count",
    "devicesTierUnit": "dev.",
    "devicePriceTiersLabel": "Tiers (extra/month over base):",
    "noDeviceTiersHint": "No tiers set. Add tiers to enable non-linear pricing.",
```
zh.json:
```json
    "devicePriceTiersTitle": "设备价格等级",
    "deviceCountLabel": "设备数量",
    "devicesTierUnit": "台",
    "devicePriceTiersLabel": "等级（每月基础价之上的附加费）：",
    "noDeviceTiersHint": "未设置等级。添加等级以启用分级定价。",
```
fa.json:
```json
    "devicePriceTiersTitle": "سطوح قیمت دستگاه",
    "deviceCountLabel": "تعداد دستگاه",
    "devicesTierUnit": "دستگاه",
    "devicePriceTiersLabel": "سطوح (هزینه ماهانه اضافی روی پایه):",
    "noDeviceTiersHint": "سطحی تنظیم نشده است. برای فعال‌سازی قیمت‌گذاری سطحی، سطح اضافه کنید.",
```

- [ ] **Step 7: Build the frontend**

Run: `cd bedolaga-cabinet && npm run build`
Expected: build succeeds (no TS errors). `PlusIcon`/`TrashIcon`/`createNumberInputHandler`/`toNumber` are already imported (used by the traffic editor); no new imports needed.

- [ ] **Step 8: Commit**

```bash
git add bedolaga-cabinet/src/api/tariffs.ts bedolaga-cabinet/src/pages/AdminTariffCreate.tsx bedolaga-cabinet/src/locales/ru.json bedolaga-cabinet/src/locales/en.json bedolaga-cabinet/src/locales/zh.json bedolaga-cabinet/src/locales/fa.json
git commit -m "feat(cabinet-ui): device price tiers editor"
```

---

## Task 11: Full regression + end-to-end verification

- [ ] **Step 1: Run the full backend test suite**

Run: `python -m pytest tests/ -q`
Expected: all pass (new tier tests + existing device/pricing tests unchanged).

- [ ] **Step 2: End-to-end (manual, per run skill)**

1. Cabinet: create tariff "Устройства", `traffic_limit_gb=0`, `device_limit=1`, period 30d = 3000 kopeks, device tiers `3→4000, 5→7000`, leave flat device price empty.
2. Bot: buy it (1 device, 30 ₽). Open "Изменение устройств": 3 → +40 ₽, 5 → +70 ₽, 4 → +55 ₽ (full remaining month).
3. Confirm 5 devices → balance charged ~70 ₽ for the remaining month.
4. Renew → renewal total = base period price + 7000×months.
5. Existing tariff WITHOUT tiers: device add still charges the old flat `device_price_kopeks` (regression check).

- [ ] **Step 3: Final commit (if any verification tweaks)**

```bash
git add -A
git commit -m "test: device tier pricing regression pass"
```

---

## Self-review notes

- **Spec coverage:** model+migration (T1-2), helper/interpolation (T1), pricing engine (T4), bot add/change (T5), keyboards (T6), bot admin config (T7), cabinet API (T8), miniapp (T9), React + locales (T10), backward-compat (empty-tier fallback in T1/T4/T5/T9), tests (T1,4,7,8,11). All spec sections mapped.
- **Out of scope (per spec):** classic-mode `PRICE_PER_DEVICE` stays linear (untouched in `_calculate_classic_core`); `_wl` and unlimited-GB unchanged.
- **Type consistency:** helper `get_device_extra_price_per_month` used identically in T4/T5/T6/T9. Field `device_price_tiers` (`dict[str,int]` / `Record<string,number>`) consistent across model, crud, schema, route, api types. Tier keys are strings everywhere (DB JSON, parser, React).
- **Known follow-ups (not blocking):** webapi `subscriptions.py` device-price exposure (spec §5 "optional, not MVP") deferred; classic-mode tiering deferred by design.