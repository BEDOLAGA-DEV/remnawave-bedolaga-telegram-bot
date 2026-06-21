# Flexible Custom-Days (Floor-Anchor Pricing) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users enter an arbitrary number of days at tariff purchase, priced from the tariff's `period_prices` anchors via a floor-anchor per-day rate capped at the next anchor (monotonic), combining with the existing device-count tiers.

**Architecture:** New `Tariff.flexible_days_enabled` flag + `Tariff.get_price_for_days_anchored(days)` method (the only place the floor-anchor math lives). The pricing engine uses it to resolve the base price for non-anchor day counts. A "✏️ Свой срок" button on the period screen opens a text-input FSM state; the typed day count is clamped and rendered on the same confirm screen (with device −/＋) via an extracted `build_period_confirm` helper. `confirm_tariff_purchase` validation is relaxed to accept any in-range day count for flexible tariffs.

**Tech Stack:** Python 3.13, aiogram v3, SQLAlchemy, Alembic, pytest. Run via `.venv\Scripts\python.exe`.

**Spec:** [docs/superpowers/specs/2026-06-21-flexible-days-anchor-pricing-design.md](../specs/2026-06-21-flexible-days-anchor-pricing-design.md)

**Branch:** `feat/flexible-days-pricing`

**Environment note:** Use `.venv\Scripts\python.exe` (Python 3.13); bare `python` is 3.10 and cannot import the app. git + python via PowerShell. `period_prices` values are in **kopeks** (e.g. `{"30": 3000}` = 30 ₽).

---

## Task 1: Model flag + floor-anchor price method

**Files:**
- Modify: `app/database/models.py` (Tariff: column after `device_price_tiers` ~line 1773; method after `get_device_extra_price_per_month` ~line 1911)
- Test: `tests/database/test_flexible_days_pricing.py`

- [ ] **Step 1: Write the failing test**

Create `tests/database/test_flexible_days_pricing.py`:

```python
from app.database.models import Tariff


def _t(periods):
    t = Tariff(name='t', device_limit=1)
    t.period_prices = periods
    return t


GRID = {'30': 3000, '90': 7000, '180': 10000}  # kopeks; 30/70/100 ₽


def test_exact_anchors():
    t = _t(GRID)
    assert t.get_price_for_days_anchored(30) == 3000
    assert t.get_price_for_days_anchored(90) == 7000
    assert t.get_price_for_days_anchored(180) == 10000


def test_between_anchors_floor_rate():
    t = _t(GRID)
    assert t.get_price_for_days_anchored(50) == 5000   # 50 * (3000/30) = 5000
    assert t.get_price_for_days_anchored(120) == 9300  # 120 * (7000/90) = 9333 -> round ruble 9300


def test_cap_at_next_anchor():
    t = _t(GRID)
    assert t.get_price_for_days_anchored(80) == 7000   # 80*100=8000 capped at ceil(90)=7000
    assert t.get_price_for_days_anchored(179) == 10000  # 179*77.7=13922 capped at ceil(180)=10000


def test_clamp_out_of_range():
    t = _t(GRID)
    assert t.get_price_for_days_anchored(10) == 3000   # clamp up to 30
    assert t.get_price_for_days_anchored(999) == 10000  # clamp down to 180


def test_single_anchor():
    t = _t({'30': 3000})
    assert t.get_price_for_days_anchored(45) == 3000   # clamped to the only anchor
    assert t.get_price_for_days_anchored(20) == 3000


def test_empty_prices():
    t = _t({})
    assert t.get_price_for_days_anchored(50) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/database/test_flexible_days_pricing.py -v`
Expected: FAIL — `AttributeError: 'Tariff' object has no attribute 'get_price_for_days_anchored'`.

- [ ] **Step 3: Add the column**

In `app/database/models.py`, immediately after the `device_price_tiers = Column(JSON, default=dict)` line (~1773), add:

```python
    # Произвольный срок с ценой floor-anchor из period_prices. Отдельно от flat custom_days.
    flexible_days_enabled = Column(Boolean, default=False, nullable=False, server_default='false')
```

- [ ] **Step 4: Add the method**

In `app/database/models.py`, immediately after the `get_device_extra_price_per_month` method (after ~line 1911), add:

```python
    def get_price_for_days_anchored(self, days: int) -> int:
        """База в копейках за произвольный срок по floor-anchor с капом следующим якорем.

        rate = price[floor]/floor_days; base = min(days × rate, price[ceil]); округление до рубля.
        days клампится в [min_anchor, max_anchor]. Пустые period_prices → 0.
        """
        prices = self.period_prices or {}
        anchors = sorted((int(d), int(p)) for d, p in prices.items())
        if not anchors:
            return 0

        d = max(anchors[0][0], min(int(days), anchors[-1][0]))

        for a_days, a_price in anchors:
            if a_days == d:
                return a_price

        floor = anchors[0]
        ceil = None
        for a_days, a_price in anchors:
            if a_days < d:
                floor = (a_days, a_price)
            elif a_days > d:
                ceil = (a_days, a_price)
                break

        rate = floor[1] / floor[0]
        raw = d * rate
        if ceil is not None:
            raw = min(raw, ceil[1])
        return int(round(raw / 100.0) * 100)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/database/test_flexible_days_pricing.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```
git add app/database/models.py tests/database/test_flexible_days_pricing.py
git commit -m "feat(tariff): flexible_days_enabled flag + floor-anchor price method"
```

---

## Task 2: Migration 0118

**Files:**
- Create: `migrations/alembic/versions/0118_add_flexible_days_enabled_to_tariffs.py`

- [ ] **Step 1: Create the migration**

Create the file (mirrors 0117, idempotent):

```python
"""add flexible_days_enabled to tariffs

Adds a per-tariff flag enabling arbitrary-day purchase priced from period_prices
anchors (floor-anchor with cap). Separate from the flat custom_days mechanism.

Revision ID: 0118
Revises: 0117
Create Date: 2026-06-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0118'
down_revision: Union[str, None] = '0117'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = {c['name'] for c in inspector.get_columns('tariffs')}

    if 'flexible_days_enabled' not in existing:
        op.add_column(
            'tariffs',
            sa.Column('flexible_days_enabled', sa.Boolean(), nullable=False, server_default='false'),
        )

    op.execute('UPDATE tariffs SET flexible_days_enabled = false WHERE flexible_days_enabled IS NULL')


def downgrade() -> None:
    op.drop_column('tariffs', 'flexible_days_enabled')
```

- [ ] **Step 2: Verify the revision chain**

Run: `.venv\Scripts\python.exe -m alembic heads`
Expected: `0118 (head)`.
Run: `.venv\Scripts\python.exe -m alembic history -r 0117:0118`
Expected: shows `0117 -> 0118`.

(The dev SQLite DB cannot run `upgrade` from scratch — migration 0001 uses JSONB; production is Postgres and applies on startup. Validating the chain + import is sufficient here.)

- [ ] **Step 3: Verify import**

Run: `.venv\Scripts\python.exe -c "import importlib.util; spec = importlib.util.spec_from_file_location('m', 'migrations/alembic/versions/0118_add_flexible_days_enabled_to_tariffs.py'); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); print('migration import OK', mod.revision, mod.down_revision)"`
Expected: `migration import OK 0118 0117`.

- [ ] **Step 4: Commit**

```
git add migrations/alembic/versions/0118_add_flexible_days_enabled_to_tariffs.py
git commit -m "feat(db): migration 0118 add flexible_days_enabled"
```

---

## Task 3: CRUD create/update param

**Files:**
- Modify: `app/database/crud/tariff.py` (`create_tariff`, `update_tariff`)

- [ ] **Step 1: Add to `create_tariff`**

In the `create_tariff` signature, after `device_price_tiers: dict[str, int] | None = None,` add:

```python
    flexible_days_enabled: bool = False,
```

In the `Tariff(...)` constructor, after `device_price_tiers=device_price_tiers or {},` add:

```python
        flexible_days_enabled=flexible_days_enabled,
```

- [ ] **Step 2: Add to `update_tariff`**

In the `update_tariff` signature, after `device_price_tiers: dict[str, int] | None = None,` add:

```python
    flexible_days_enabled: bool | None = None,
```

In the body, after the `if device_price_tiers is not None: tariff.device_price_tiers = device_price_tiers` block add:

```python
    if flexible_days_enabled is not None:
        tariff.flexible_days_enabled = flexible_days_enabled
```

- [ ] **Step 3: Verify import**

Run: `.venv\Scripts\python.exe -c "import app.database.crud.tariff; print('import OK')"`
Expected: `import OK`.

- [ ] **Step 4: Commit**

```
git add app/database/crud/tariff.py
git commit -m "feat(crud): flexible_days_enabled param in create/update_tariff"
```

---

## Task 4: Engine base price for flexible days

**Files:**
- Modify: `app/services/pricing_engine.py` (`_calculate_tariff_core` base block ~574-585)
- Test: `tests/services/test_engine_flexible_days.py`

- [ ] **Step 1: Inject the flexible base resolution**

Current base block (~574-585):
```python
        # --- Base price ---
        is_daily = getattr(tariff, 'is_daily', False)
        if is_daily and period_days <= 1:
            base_price = int(getattr(tariff, 'daily_price_kopeks', 0) or 0)
        else:
            period_prices: dict = tariff.period_prices or {}
            base_price = int(period_prices.get(str(period_days), 0) or 0)
            if base_price == 0 and hasattr(tariff, 'get_price_for_custom_days'):
                if hasattr(tariff, 'can_purchase_custom_days') and tariff.can_purchase_custom_days():
                    custom_price = tariff.get_price_for_custom_days(period_days)
                    if custom_price is not None:
                        base_price = int(custom_price)
```
Replace with (add the flexible-days branch BEFORE the flat-custom-days fallback):
```python
        # --- Base price ---
        is_daily = getattr(tariff, 'is_daily', False)
        if is_daily and period_days <= 1:
            base_price = int(getattr(tariff, 'daily_price_kopeks', 0) or 0)
        else:
            period_prices: dict = tariff.period_prices or {}
            base_price = int(period_prices.get(str(period_days), 0) or 0)
            if base_price == 0 and getattr(tariff, 'flexible_days_enabled', False):
                _periods = tariff.get_available_periods()
                if _periods and _periods[0] <= period_days <= _periods[-1]:
                    base_price = int(tariff.get_price_for_days_anchored(period_days))
            if base_price == 0 and hasattr(tariff, 'get_price_for_custom_days'):
                if hasattr(tariff, 'can_purchase_custom_days') and tariff.can_purchase_custom_days():
                    custom_price = tariff.get_price_for_custom_days(period_days)
                    if custom_price is not None:
                        base_price = int(custom_price)
```

- [ ] **Step 2: Write the test**

Create `tests/services/test_engine_flexible_days.py`:

```python
import pytest

from app.database.models import Tariff
from app.services.pricing_engine import pricing_engine


def _t():
    t = Tariff(name='t', device_limit=1)
    t.period_prices = {'30': 3000, '90': 7000, '180': 10000}
    t.flexible_days_enabled = True
    t.device_price_kopeks = None
    t.device_price_tiers = {}
    t.is_daily = False
    return t


@pytest.mark.asyncio
async def test_engine_flexible_base_for_custom_day():
    t = _t()
    # 50 days -> floor-anchor base 5000 kopeks; no extra devices (device_limit=1)
    result = await pricing_engine.calculate_tariff_purchase_price(t, 50, device_limit=1)
    assert result.base_price == 5000
```

(If the repo's async test style differs, match `tests/cabinet/subscription/test_traffic_pricing.py`. The key assertion is `base_price == 5000` for 50 days.)

- [ ] **Step 3: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/services/test_engine_flexible_days.py -v`
Expected: PASS.
Run: `.venv\Scripts\python.exe -m pytest tests/cabinet/subscription/test_traffic_pricing.py -q`
Expected: existing pricing tests still pass.

- [ ] **Step 4: Commit**

```
git add app/services/pricing_engine.py tests/services/test_engine_flexible_days.py
git commit -m "feat(pricing): flexible-days floor-anchor base price"
```

---

## Task 5: "Свой срок" button + open handler

**Files:**
- Modify: `app/handlers/subscription/tariff_purchase.py` (`get_tariff_periods_keyboard` ~255-292; new handler before `select_tariff_period` ~1338; registration ~4640+)

- [ ] **Step 1: Add the button to the period keyboard**

In `get_tariff_periods_keyboard`, replace the final back-button + return:
```python
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)])

    return InlineKeyboardMarkup(inline_keyboard=buttons)
```
with:
```python
    if getattr(tariff, 'flexible_days_enabled', False) and (tariff.period_prices or {}):
        buttons.append(
            [InlineKeyboardButton(text='✏️ Свой срок', callback_data=f'nz!_tariff_flexdays:{tariff.id}')]
        )

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)])

    return InlineKeyboardMarkup(inline_keyboard=buttons)
```

- [ ] **Step 2: Add the open handler**

Immediately before `async def select_tariff_period` (~line 1338), add:

```python
@error_handler
async def handle_tariff_flexdays_start(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Кнопка «Свой срок»: просим ввести число дней текстом."""
    tariff_id = int(callback.data.split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active or not getattr(tariff, 'flexible_days_enabled', False):
        await callback.answer('Недоступно', show_alert=True)
        return

    periods = tariff.get_available_periods()
    if not periods:
        await callback.answer('Период недоступен', show_alert=True)
        return
    min_d, max_d = periods[0], periods[-1]

    await state.set_state(SubscriptionStates.selecting_custom_days)
    await state.update_data(selected_tariff_id=tariff_id, flexdays_min=min_d, flexdays_max=max_d)

    await callback.message.edit_text(
        f'✏️ <b>Свой срок</b>\n\n'
        f'Введите число дней от <b>{min_d}</b> до <b>{max_d}</b> сообщением.\n'
        f'Цена считается по тарифной сетке.',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=get_texts(db_user.language).BACK, callback_data=f'nz!_tariff_select:{tariff_id}')]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()
```

- [ ] **Step 3: Register the callback**

In `register_tariff_purchase_handlers`, right after the `handle_tariff_device_change` registration, add:

```python
    dp.callback_query.register(handle_tariff_flexdays_start, F.data.startswith('nz!_tariff_flexdays:'))
```

- [ ] **Step 4: Verify import**

Run: `.venv\Scripts\python.exe -c "import app.handlers.subscription.tariff_purchase as m; print('import OK', hasattr(m, 'handle_tariff_flexdays_start'))"`
Expected: `import OK True`.

- [ ] **Step 5: Commit**

```
git add app/handlers/subscription/tariff_purchase.py
git commit -m "feat(purchase): flexible-days button + open handler"
```

---

## Task 6: Extract `build_period_confirm`

**Files:**
- Modify: `app/handlers/subscription/tariff_purchase.py` (`select_tariff_period` body ~1358-1480)

- [ ] **Step 1: Add `build_period_confirm` and slim `select_tariff_period`**

`select_tariff_period`'s body after the tariff fetch/validate currently renders the confirm screen by editing the message. Extract that body into a helper that RETURNS `(text, keyboard)` and does NOT touch the callback. Add this new function immediately before `async def select_tariff_period` (and before `handle_tariff_flexdays_start` from Task 5 is fine too — order among module functions does not matter):

```python
async def build_period_confirm(
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    tariff: Tariff,
    period: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Готовит (текст, клавиатуру) экрана подтверждения покупки на `period` дней.

    Сохраняет state + корзину (при нехватке баланса). Не трогает callback/message —
    подходит и для CallbackQuery.edit_text, и для Message.answer.
    """
    tariff_id = tariff.id
    group_pct, offer_pct, discount_percent = _get_user_period_discount(db_user, period)
    scheduled_pct = await _get_scheduled_promo_discount(db, tariff_id)
    if scheduled_pct > 0:
        remaining = (100 - scheduled_pct) * (100 - discount_percent)
        discount_percent = 100 - remaining // 100

    selectable, base, effective_max = _tariff_device_purchase_options(tariff)
    data = await state.get_data()
    selected_device_limit = data.get('selected_device_limit')
    if selected_device_limit is None:
        selected_device_limit = base
    selected_device_limit = max(base, min(int(selected_device_limit), effective_max))

    from app.services.pricing_engine import pricing_engine

    result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period,
        device_limit=selected_device_limit,
        user=db_user,
        db=db,
    )
    final_price = result.final_total
    user_balance = db_user.balance_kopeks or 0
    traffic = format_traffic(tariff.traffic_limit_gb)
    single_tariff = bool(data.get('single_tariff'))
    back_cb = 'nz!_back_to_menu' if single_tariff else f'nz!_tariff_select:{tariff_id}'

    if user_balance >= final_price:
        discount_text = f'\n🎁 Скидка: {discount_percent}%' if discount_percent > 0 else ''
        text = (
            f'✅ <b>Подтверждение покупки</b>\n\n'
            f'📦 Тариф: <b>{html.escape(tariff.name)}</b>\n'
            f'📊 Трафик: {traffic}\n'
            f'📱 Устройств: {selected_device_limit}\n'
            f'📅 Период: {format_period(period)}\n'
            f'{discount_text}\n'
            f'💰 <b>Итого: {format_price_kopeks(final_price)}</b>\n\n'
            f'💳 Ваш баланс: {format_price_kopeks(user_balance)}\n'
            f'После оплаты: {format_price_kopeks(user_balance - final_price)}'
        )
        kb = get_tariff_confirm_keyboard(
            tariff_id,
            period,
            db_user.language,
            device_limit=selected_device_limit,
            base=base,
            effective_max=effective_max,
            devices_selectable=selectable,
            back_callback=back_cb,
        )
    else:
        missing = final_price - user_balance
        if settings.is_multi_tariff_enabled():
            from app.database.crud.subscription import get_subscription_by_user_and_tariff

            _existing_sub = await get_subscription_by_user_and_tariff(db, db_user.id, tariff_id)
        else:
            _existing_sub = await get_subscription_by_user_id(db, db_user.id)

        cart_data = {
            'cart_mode': 'tariff_purchase',
            'tariff_id': tariff_id,
            'period_days': period,
            'total_price': final_price,
            'user_id': db_user.id,
            'saved_cart': True,
            'missing_amount': missing,
            'return_to_cart': True,
            'description': f'Покупка тарифа {tariff.name} на {period} дней',
            'traffic_limit_gb': tariff.traffic_limit_gb,
            'device_limit': selected_device_limit,
            'allowed_squads': tariff.allowed_squads or [],
            'discount_percent': discount_percent,
            'subscription_id': _existing_sub.id if _existing_sub else None,
        }
        await user_cart_service.save_user_cart(db_user.id, cart_data)
        text = (
            f'❌ <b>Недостаточно средств</b>\n\n'
            f'📦 Тариф: <b>{html.escape(tariff.name)}</b>\n'
            f'📅 Период: {format_period(period)}\n'
            f'💰 Стоимость: {format_price_kopeks(final_price)}\n\n'
            f'💳 Ваш баланс: {format_price_kopeks(user_balance)}\n'
            f'⚠️ Не хватает: <b>{format_price_kopeks(missing)}</b>\n\n'
            f'🛒 <i>Корзина сохранена! После пополнения баланса подписка будет оформлена автоматически.</i>'
        )
        kb = get_tariff_insufficient_balance_keyboard(tariff_id, period, db_user.language)

    target_subscription_id: int | None = None
    if settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_subscription_by_user_and_tariff

        _existing_for_pin = await get_subscription_by_user_and_tariff(db, db_user.id, tariff_id)
        target_subscription_id = _existing_for_pin.id if _existing_for_pin else None

    await state.update_data(
        selected_tariff_id=tariff_id,
        selected_period=period,
        final_price=final_price,
        tariff_discount_percent=discount_percent,
        target_subscription_id=target_subscription_id,
        selected_device_limit=selected_device_limit,
    )
    return text, kb
```

Then replace the entire body of `select_tariff_period` AFTER the tariff fetch/validate block (from `# Получаем скидку для выбранного периода (для бейджа)` through the final `await callback.answer()`) with:

```python
    text, kb = await build_period_confirm(db_user, db, state, tariff, period)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode='HTML')
    await callback.answer()
```

- [ ] **Step 2: Verify import**

Run: `.venv\Scripts\python.exe -c "import app.handlers.subscription.tariff_purchase as m; print('import OK', hasattr(m, 'build_period_confirm'))"`
Expected: `import OK True`.

- [ ] **Step 3: Regression — device −/＋ guard + options still hold**

Run: `.venv\Scripts\python.exe -m pytest tests/handlers/test_no_callback_data_mutation.py tests/handlers/test_tariff_device_options.py -q`
Expected: PASS (the refactor must not reintroduce `callback.data =`).

- [ ] **Step 4: Commit**

```
git add app/handlers/subscription/tariff_purchase.py
git commit -m "refactor(purchase): extract build_period_confirm from select_tariff_period"
```

---

## Task 7: Text-input handler for custom days

**Files:**
- Modify: `app/handlers/subscription/tariff_purchase.py` (new handler after `handle_tariff_flexdays_start`; registration ~4640+)

- [ ] **Step 1: Add the text-input handler**

Immediately after `handle_tariff_flexdays_start`, add:

```python
@error_handler
async def process_flexdays_input(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """Текстовый ввод числа дней для flexible-тарифа → экран подтверждения."""
    data = await state.get_data()
    tariff_id = data.get('selected_tariff_id')
    tariff = await get_tariff_by_id(db, tariff_id) if tariff_id else None
    if not tariff or not tariff.is_active or not getattr(tariff, 'flexible_days_enabled', False):
        await message.answer('Тариф недоступен')
        await state.clear()
        return

    periods = tariff.get_available_periods()
    if not periods:
        await message.answer('Период недоступен')
        await state.clear()
        return
    min_d, max_d = periods[0], periods[-1]

    raw = (message.text or '').strip()
    try:
        days = int(raw)
    except ValueError:
        await message.answer(f'Введите число дней от {min_d} до {max_d}.')
        return

    days = max(min_d, min(days, max_d))

    await state.set_state(None)
    text, kb = await build_period_confirm(db_user, db, state, tariff, days)
    await message.answer(text, reply_markup=kb, parse_mode='HTML')
```

- [ ] **Step 2: Register the message handler**

In `register_tariff_purchase_handlers`, after the `handle_tariff_flexdays_start` registration (Task 5), add:

```python
    dp.message.register(process_flexdays_input, SubscriptionStates.selecting_custom_days, F.text)
```

Confirm `SubscriptionStates` is imported in this file. If not, add `from app.states import SubscriptionStates` near the other imports.

- [ ] **Step 3: Verify import**

Run: `.venv\Scripts\python.exe -c "import app.handlers.subscription.tariff_purchase as m; print('import OK', hasattr(m, 'process_flexdays_input'))"`
Expected: `import OK True`.

- [ ] **Step 4: Commit**

```
git add app/handlers/subscription/tariff_purchase.py
git commit -m "feat(purchase): flexible-days text input -> confirm screen"
```

---

## Task 8: Relax confirm validation for flexible days

**Files:**
- Modify: `app/handlers/subscription/tariff_purchase.py` (`confirm_tariff_purchase` validation ~1526-1529)

- [ ] **Step 1: Replace the validation block**

Current (~1526-1529):
```python
    # Validate period is available for this tariff
    if str(period) not in (tariff.period_prices or {}):
        await callback.answer('Период недоступен', show_alert=True)
        return
```
Replace with:
```python
    # Validate period: exact anchor, or any in-range day for flexible-days tariffs
    _periods = tariff.get_available_periods()
    _flex_ok = (
        getattr(tariff, 'flexible_days_enabled', False)
        and _periods
        and _periods[0] <= period <= _periods[-1]
    )
    if str(period) not in (tariff.period_prices or {}) and not _flex_ok:
        await callback.answer('Период недоступен', show_alert=True)
        return
```

- [ ] **Step 2: Verify import**

Run: `.venv\Scripts\python.exe -c "import app.handlers.subscription.tariff_purchase; print('import OK')"`
Expected: `import OK`.

- [ ] **Step 3: Commit**

```
git add app/handlers/subscription/tariff_purchase.py
git commit -m "feat(purchase): accept in-range custom days at confirm for flexible tariffs"
```

---

## Task 9: Regression + end-to-end

- [ ] **Step 1: Targeted test run**

Run: `.venv\Scripts\python.exe -m pytest tests/database/test_flexible_days_pricing.py tests/services/test_engine_flexible_days.py tests/handlers/test_no_callback_data_mutation.py tests/handlers/test_tariff_device_options.py tests/cabinet/subscription -q`
Expected: all pass.

- [ ] **Step 2: Import sanity**

Run: `.venv\Scripts\python.exe -c "import app.handlers.subscription.tariff_purchase, app.handlers.subscription.purchase, app.services.pricing_engine; print('import OK')"`
Expected: `import OK`.

- [ ] **Step 3: End-to-end (manual, per run skill)**

Give the "Безлимит" tariff ≥2 periods (e.g. 30=3000, 90=7000, 180=10000) and set `flexible_days_enabled=true` in the DB. Then in the bot:
1. Buy menu → period screen shows anchor buttons + "✏️ Свой срок".
2. Tap "Свой срок" → enter `50` → confirm shows 50 days, base 50 ₽; device −/＋ adds on top.
3. Enter `120` → ~93 ₽; `179` → 100 ₽ (capped); `999` → clamps to 180 (100 ₽); `abc` → re-prompt.
4. Confirm purchase → subscription created for the chosen day count.
5. Anchor buttons (30/90/180) still buy exact-anchor prices.

- [ ] **Step 4: Final commit (if tweaks)**

```
git add -A
git commit -m "test: flexible-days regression pass"
```

---

## Self-review notes

- **Spec coverage:** floor-anchor+cap method (T1), migration (T2), crud (T3), engine base (T4), button + open handler (T5), confirm render reachable from Message via `build_period_confirm` (T6), text input (T7), confirm validation relax (T8), tests (T1/T4/T9). All spec sections mapped.
- **Out of scope (per spec):** admin-UI for the flag (set via DB), custom-traffic combine, daily, old flat custom-days untouched.
- **Type consistency:** `get_price_for_days_anchored(days) -> int` (kopeks) used in T1/T4. `flexible_days_enabled` consistent across model/crud/engine/keyboard/handlers. `build_period_confirm(db_user, db, state, tariff, period) -> (text, kb)` used in T6 (`select_tariff_period`) and T7 (`process_flexdays_input`). Callback `nz!_tariff_flexdays:{id}` emitted in T5 keyboard, parsed in T5 handler, registered in T5. State `SubscriptionStates.selecting_custom_days` set in T5, consumed in T7.
- **Known nuance resolved:** Message→confirm render uses extracted `build_period_confirm` (returns text+kb), so `process_flexdays_input` does `message.answer(...)` while `select_tariff_period` does `callback.message.edit_text(...)`. No callback mutation.
- **Device combine:** engine computes device extra via `get_device_extra_price_per_month × calculate_months_from_days(D)` automatically once the flexible base is resolved — no extra engine change.
