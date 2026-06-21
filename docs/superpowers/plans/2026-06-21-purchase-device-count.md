# Purchase Device-Count Selection + Single-Tariff Skip — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In the tariff purchase flow, skip the "Выберите тариф" screen when only one active tariff exists, and let the user choose the initial device count (−/＋ on the confirm screen) priced by `device_price_tiers`.

**Architecture:** All changes live in `app/handlers/subscription/tariff_purchase.py` plus its keyboard helpers. A single helper `_tariff_device_purchase_options(tariff)` decides selectability/bounds. The confirm screen prices via `pricing_engine.calculate_tariff_purchase_price(device_limit=N)` so preview == charge. The chosen count travels in FSM state (`selected_device_limit`); a new `nz!_tariff_dev` callback re-renders the confirm screen.

**Tech Stack:** Python 3.13, aiogram v3, SQLAlchemy, pytest. Run via `.venv\Scripts\python.exe`.

**Spec:** [docs/superpowers/specs/2026-06-21-purchase-device-count-and-single-tariff-skip-design.md](../specs/2026-06-21-purchase-device-count-and-single-tariff-skip-design.md)

**Branch:** `feat/purchase-device-count`

**Environment note:** Use `.venv\Scripts\python.exe` (Python 3.13). Bare `python` is 3.10 and cannot import the app. git + python via PowerShell.

---

## Task 1: Device-purchase options helper

**Files:**
- Modify: `app/handlers/subscription/tariff_purchase.py` (add helper near the other module-level helpers, after `_get_scheduled_promo_discount` ~line 100)
- Test: `tests/handlers/test_tariff_device_options.py`

- [ ] **Step 1: Write the failing test**

Create `tests/handlers/test_tariff_device_options.py`:

```python
from app.database.models import Tariff
from app.handlers.subscription.tariff_purchase import _tariff_device_purchase_options


def _t(device_limit=1, max_device_limit=None, tiers=None, price=None):
    t = Tariff(name='t', device_limit=device_limit)
    t.max_device_limit = max_device_limit
    t.device_price_tiers = tiers if tiers is not None else {}
    t.device_price_kopeks = price
    return t


def test_selectable_with_tiers_and_max():
    t = _t(1, 5, tiers={'3': 4000, '5': 7000})
    assert _tariff_device_purchase_options(t) == (True, 1, 5)


def test_not_selectable_without_price():
    t = _t(1, 5)  # no tiers, no price
    assert _tariff_device_purchase_options(t) == (False, 1, 5)


def test_not_selectable_when_max_not_above_base():
    t = _t(1, 1, tiers={'3': 4000})
    assert _tariff_device_purchase_options(t) == (False, 1, 1)


def test_linear_price_makes_selectable():
    t = _t(1, 3, price=500)
    assert _tariff_device_purchase_options(t) == (True, 1, 3)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/handlers/test_tariff_device_options.py -v`
Expected: FAIL — `ImportError: cannot import name '_tariff_device_purchase_options'`.

- [ ] **Step 3: Add the helper**

In `app/handlers/subscription/tariff_purchase.py`, after the `_get_scheduled_promo_discount` function (~line 100), add (`settings` is already imported in this module):

```python
def _tariff_device_purchase_options(tariff) -> tuple[bool, int, int]:
    """Опции выбора устройств при покупке тарифа.

    Возвращает (selectable, base, effective_max):
      - selectable: показывать ли −/＋ (есть цена устройств И есть запас сверх базы)
      - base: включённые устройства (device_limit, минимум 1)
      - effective_max: верхняя граница выбора
    """
    base = tariff.device_limit or 1
    has_price = bool(getattr(tariff, 'device_price_tiers', None)) or (
        getattr(tariff, 'device_price_kopeks', None) or 0
    ) > 0
    raw_max = getattr(tariff, 'max_device_limit', None) or (
        settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else 0
    )
    effective_max = raw_max if raw_max and raw_max > base else base
    selectable = has_price and effective_max > base
    return selectable, base, effective_max
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/handlers/test_tariff_device_options.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```
git add app/handlers/subscription/tariff_purchase.py tests/handlers/test_tariff_device_options.py
git commit -m "feat(purchase): device-purchase options helper"
```

---

## Task 2: Confirm keyboard device row + back_callback params

**Files:**
- Modify: `app/handlers/subscription/tariff_purchase.py` (`get_tariff_confirm_keyboard` ~310-322; `get_tariff_periods_keyboard` ~235-271; `get_tariff_periods_keyboard_with_traffic` ~274-307)

- [ ] **Step 1: Replace `get_tariff_confirm_keyboard`**

Current (~310-322):
```python
def get_tariff_confirm_keyboard(
    tariff_id: int,
    period: int,
    language: str,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру подтверждения покупки тарифа."""
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='✅ Подтвердить покупку', callback_data=f'nz!_tariff_confirm:{tariff_id}:{period}')],
            [InlineKeyboardButton(text=texts.BACK, callback_data=f'nz!_tariff_select:{tariff_id}')],
        ]
    )
```
Replace with:
```python
def get_tariff_confirm_keyboard(
    tariff_id: int,
    period: int,
    language: str,
    *,
    device_limit: int = 1,
    base: int = 1,
    effective_max: int = 1,
    devices_selectable: bool = False,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру подтверждения покупки тарифа."""
    texts = get_texts(language)
    rows = []
    if devices_selectable:
        minus_cb = (
            f'nz!_tariff_dev:{tariff_id}:{period}:{device_limit - 1}'
            if device_limit > base
            else 'nz!_noop'
        )
        plus_cb = (
            f'nz!_tariff_dev:{tariff_id}:{period}:{device_limit + 1}'
            if device_limit < effective_max
            else 'nz!_noop'
        )
        rows.append(
            [
                InlineKeyboardButton(text='➖', callback_data=minus_cb),
                InlineKeyboardButton(text=f'📱 {device_limit} устр.', callback_data='nz!_noop'),
                InlineKeyboardButton(text='➕', callback_data=plus_cb),
            ]
        )
    rows.append(
        [InlineKeyboardButton(text='✅ Подтвердить покупку', callback_data=f'nz!_tariff_confirm:{tariff_id}:{period}')]
    )
    rows.append(
        [InlineKeyboardButton(text=texts.BACK, callback_data=back_callback or f'nz!_tariff_select:{tariff_id}')]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
```

- [ ] **Step 2: Add `back_callback` to `get_tariff_periods_keyboard`**

In `get_tariff_periods_keyboard` (~235), change the signature line:
```python
def get_tariff_periods_keyboard(
    tariff: Tariff,
    language: str,
    db_user: User | None = None,
    scheduled_pct: int = 0,
) -> InlineKeyboardMarkup:
```
to:
```python
def get_tariff_periods_keyboard(
    tariff: Tariff,
    language: str,
    db_user: User | None = None,
    scheduled_pct: int = 0,
    back_callback: str = 'nz!_tariff_list',
) -> InlineKeyboardMarkup:
```
and change its back button line (~269):
```python
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='nz!_tariff_list')])
```
to:
```python
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)])
```

- [ ] **Step 3: Add `back_callback` to `get_tariff_periods_keyboard_with_traffic`**

In `get_tariff_periods_keyboard_with_traffic` (~274), change the signature:
```python
def get_tariff_periods_keyboard_with_traffic(
    tariff: Tariff,
    language: str,
    db_user: User | None = None,
) -> InlineKeyboardMarkup:
```
to:
```python
def get_tariff_periods_keyboard_with_traffic(
    tariff: Tariff,
    language: str,
    db_user: User | None = None,
    back_callback: str = 'nz!_tariff_list',
) -> InlineKeyboardMarkup:
```
and its back button line (~305):
```python
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='nz!_tariff_list')])
```
to:
```python
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)])
```

- [ ] **Step 4: Verify import**

Run: `.venv\Scripts\python.exe -c "import app.handlers.subscription.tariff_purchase; print('import OK')"`
Expected: `import OK`.

- [ ] **Step 5: Commit**

```
git add app/handlers/subscription/tariff_purchase.py
git commit -m "feat(purchase): confirm keyboard device row + back_callback params"
```

---

## Task 3: Device-aware confirm preview (`select_tariff_period`)

**Files:**
- Modify: `app/handlers/subscription/tariff_purchase.py` (`select_tariff_period` ~1266-1384)

- [ ] **Step 1: Replace the price + render block**

In `select_tariff_period`, replace the block from `# Получаем скидку для выбранного периода` (~1282) through the `if user_balance >= final_price:` confirm render and its `else:` insufficient branch, up to (but not including) the `# Resolve target subscription_id` comment (~1363). Specifically replace lines ~1282-1361 with:

```python
    # Получаем скидку для выбранного периода (для бейджа)
    group_pct, offer_pct, discount_percent = _get_user_period_discount(db_user, period)
    scheduled_pct = await _get_scheduled_promo_discount(db, tariff_id)
    if scheduled_pct > 0:
        remaining = (100 - scheduled_pct) * (100 - discount_percent)
        discount_percent = 100 - remaining // 100

    # Выбор устройств
    selectable, base, effective_max = _tariff_device_purchase_options(tariff)
    data = await state.get_data()
    selected_device_limit = data.get('selected_device_limit')
    if selected_device_limit is None:
        selected_device_limit = base
    selected_device_limit = max(base, min(int(selected_device_limit), effective_max))

    # Единый источник цены — движок (превью == списание), включает устройства
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
        discount_text = ''
        if discount_percent > 0:
            discount_text = f'\n🎁 Скидка: {discount_percent}%'

        await callback.message.edit_text(
            f'✅ <b>Подтверждение покупки</b>\n\n'
            f'📦 Тариф: <b>{html.escape(tariff.name)}</b>\n'
            f'📊 Трафик: {traffic}\n'
            f'📱 Устройств: {selected_device_limit}\n'
            f'📅 Период: {format_period(period)}\n'
            f'{discount_text}\n'
            f'💰 <b>Итого: {format_price_kopeks(final_price)}</b>\n\n'
            f'💳 Ваш баланс: {format_price_kopeks(user_balance)}\n'
            f'После оплаты: {format_price_kopeks(user_balance - final_price)}',
            reply_markup=get_tariff_confirm_keyboard(
                tariff_id,
                period,
                db_user.language,
                device_limit=selected_device_limit,
                base=base,
                effective_max=effective_max,
                devices_selectable=selectable,
                back_callback=back_cb,
            ),
            parse_mode='HTML',
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

        await callback.message.edit_text(
            f'❌ <b>Недостаточно средств</b>\n\n'
            f'📦 Тариф: <b>{html.escape(tariff.name)}</b>\n'
            f'📅 Период: {format_period(period)}\n'
            f'💰 Стоимость: {format_price_kopeks(final_price)}\n\n'
            f'💳 Ваш баланс: {format_price_kopeks(user_balance)}\n'
            f'⚠️ Не хватает: <b>{format_price_kopeks(missing)}</b>\n\n'
            f'🛒 <i>Корзина сохранена! После пополнения баланса подписка будет оформлена автоматически.</i>',
            reply_markup=get_tariff_insufficient_balance_keyboard(tariff_id, period, db_user.language),
            parse_mode='HTML',
        )
```

- [ ] **Step 2: Persist `selected_device_limit` in state**

A few lines below, the function already calls `await state.update_data(selected_tariff_id=tariff_id, selected_period=period, final_price=final_price, tariff_discount_percent=discount_percent, target_subscription_id=target_subscription_id)`. Add `selected_device_limit=selected_device_limit,` to that call so the confirm step can read it:

```python
    await state.update_data(
        selected_tariff_id=tariff_id,
        selected_period=period,
        final_price=final_price,
        tariff_discount_percent=discount_percent,
        target_subscription_id=target_subscription_id,
        selected_device_limit=selected_device_limit,
    )
```

- [ ] **Step 3: Verify import**

Run: `.venv\Scripts\python.exe -c "import app.handlers.subscription.tariff_purchase; print('import OK')"`
Expected: `import OK`.

- [ ] **Step 4: Commit**

```
git add app/handlers/subscription/tariff_purchase.py
git commit -m "feat(purchase): device-aware confirm preview via pricing engine"
```

---

## Task 4: `nz!_tariff_dev` handler (−/＋)

**Files:**
- Modify: `app/handlers/subscription/tariff_purchase.py` (new handler after `select_tariff_period` ~1385; registration ~4523)

- [ ] **Step 1: Add the handler**

After `select_tariff_period` (right before `confirm_tariff_purchase`, ~line 1386), add:

```python
@error_handler
async def handle_tariff_device_change(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """−/＋ устройств на экране подтверждения. Сохраняет выбор и перерисовывает."""
    parts = callback.data.split(':')
    tariff_id = int(parts[1])
    period = int(parts[2])
    requested = int(parts[3])

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not tariff.is_active:
        await callback.answer('Тариф недоступен', show_alert=True)
        return

    _selectable, base, effective_max = _tariff_device_purchase_options(tariff)
    clamped = max(base, min(requested, effective_max))
    await state.update_data(selected_device_limit=clamped)

    # Перерисовываем экран подтверждения тем же рендером
    callback.data = f'nz!_tariff_period:{tariff_id}:{period}'
    await select_tariff_period(callback, db_user, db, state)
```

- [ ] **Step 2: Register the callback**

In `register_tariff_purchase_handlers` (~4523, right after the `select_tariff_period` registration), add:

```python
    dp.callback_query.register(handle_tariff_device_change, F.data.startswith('nz!_tariff_dev:'))
```

- [ ] **Step 3: Verify import + registration**

Run: `.venv\Scripts\python.exe -c "import app.handlers.subscription.tariff_purchase as m; print('import OK', hasattr(m, 'handle_tariff_device_change'))"`
Expected: `import OK True`.

- [ ] **Step 4: Commit**

```
git add app/handlers/subscription/tariff_purchase.py
git commit -m "feat(purchase): nz!_tariff_dev device +/- handler"
```

---

## Task 5: Charge the chosen device count (`confirm_tariff_purchase`)

**Files:**
- Modify: `app/handlers/subscription/tariff_purchase.py` (`confirm_tariff_purchase` device_limit ~1456-1466; new-sub creates ~1578, ~1607)

- [ ] **Step 1: Resolve chosen device_limit for new purchases**

Current (~1456-1466):
```python
    device_limit = None
    if existing_sub and existing_sub.tariff_id == tariff.id:
        device_limit = existing_sub.device_limit

    result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period,
        device_limit=device_limit,
        user=db_user,
        db=db,
    )
    final_price = result.final_total
```
Replace with:
```python
    _selectable, _base, _eff_max = _tariff_device_purchase_options(tariff)
    if existing_sub and existing_sub.tariff_id == tariff.id:
        # Продление того же тарифа — кол-во устройств не меняем здесь
        device_limit = existing_sub.device_limit
    else:
        _state_dev = await state.get_data() if state else {}
        _sel = _state_dev.get('selected_device_limit') or _base
        device_limit = max(_base, min(int(_sel), _eff_max))

    result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        period,
        device_limit=device_limit,
        user=db_user,
        db=db,
    )
    final_price = result.final_total
```

- [ ] **Step 2: Pass `device_limit` to the two NEW-subscription creates**

At the multi-tariff new-sub create (~1578-1587), change `device_limit=tariff.device_limit,` to `device_limit=device_limit,`:
```python
                subscription = await create_paid_subscription(
                    db=db,
                    user_id=db_user.id,
                    duration_days=period,
                    traffic_limit_gb=tariff.traffic_limit_gb,
                    device_limit=device_limit,
                    connected_squads=squads,
                    tariff_id=tariff.id,
                    wl_traffic_limit_gb=resolve_wl_traffic_for_tariff(tariff),
                )
```
At the legacy new-sub create (~1607-1616), change `device_limit=tariff.device_limit,` to `device_limit=device_limit,`:
```python
            subscription = await create_paid_subscription(
                db=db,
                user_id=db_user.id,
                duration_days=period,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=device_limit,
                connected_squads=squads,
                tariff_id=tariff.id,
                wl_traffic_limit_gb=resolve_wl_traffic_for_tariff(tariff),
            )
```
Leave the two `extend_subscription` branches (with `effective_device_limit = max(...)`) UNCHANGED.

- [ ] **Step 3: Verify import**

Run: `.venv\Scripts\python.exe -c "import app.handlers.subscription.tariff_purchase; print('import OK')"`
Expected: `import OK`.

- [ ] **Step 4: Commit**

```
git add app/handlers/subscription/tariff_purchase.py
git commit -m "feat(purchase): charge chosen device count on new subscription"
```

---

## Task 6: Skip "Выберите тариф" for a single tariff

**Files:**
- Modify: `app/handlers/subscription/tariff_purchase.py` (`show_tariffs_list` ~590-640; `select_tariff` ~643-811)

- [ ] **Step 1: Extract a render helper from `select_tariff`**

In `select_tariff`, the body after the "already purchased" check (the non-daily branches, ~743-808) is needed from `show_tariffs_list` too. Extract it into a new async helper placed right before `select_tariff` (~642):

```python
async def _render_tariff_entry(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    tariff: Tariff,
):
    """Рендерит первый экран НЕ-суточного тарифа (период/custom).

    Учитывает single_tariff из state для кнопки «Назад» на экране периода.
    Суточные тарифы обрабатывает select_tariff (сюда не попадают).
    """
    tariff_id = tariff.id
    single_tariff = bool((await state.get_data()).get('single_tariff'))
    periods_back = 'nz!_back_to_menu' if single_tariff else 'nz!_tariff_list'

    can_custom_days = tariff.can_purchase_custom_days()
    can_custom_traffic = tariff.can_purchase_custom_traffic()

    if can_custom_days:
        user_balance = db_user.balance_kopeks or 0
        initial_days = tariff.min_days
        initial_traffic = tariff.min_traffic_gb if can_custom_traffic else tariff.traffic_limit_gb
        group_pct, offer_pct, discount_percent = _get_user_period_discount(db_user, initial_days)
        await state.update_data(
            selected_tariff_id=tariff_id,
            custom_days=initial_days,
            custom_traffic_gb=initial_traffic,
            period_discount_percent=discount_percent,
            period_group_pct=group_pct,
            period_offer_pct=offer_pct,
        )
        preview_text = await format_custom_tariff_preview(
            tariff=tariff,
            days=initial_days,
            traffic_gb=initial_traffic,
            user_balance=user_balance,
            db_user=db_user,
            discount_percent=discount_percent,
        )
        await callback.message.edit_text(
            preview_text,
            reply_markup=get_custom_tariff_keyboard(
                tariff_id=tariff_id,
                language=db_user.language,
                days=initial_days,
                traffic_gb=initial_traffic,
                can_custom_days=can_custom_days,
                can_custom_traffic=can_custom_traffic,
                min_days=tariff.min_days,
                max_days=tariff.max_days,
                min_traffic=tariff.min_traffic_gb,
                max_traffic=tariff.max_traffic_gb,
            ),
            parse_mode='HTML',
        )
    elif can_custom_traffic:
        await callback.message.edit_text(
            format_tariff_info_for_user(tariff, db_user.language)
            + '\n\n📊 <i>После выбора периода вы сможете настроить трафик</i>',
            reply_markup=get_tariff_periods_keyboard_with_traffic(
                tariff, db_user.language, db_user=db_user, back_callback=periods_back
            ),
            parse_mode='HTML',
        )
    else:
        _scheduled = await _get_scheduled_promo_discount(db, tariff_id)
        await callback.message.edit_text(
            format_tariff_info_for_user(tariff, db_user.language),
            reply_markup=get_tariff_periods_keyboard(
                tariff, db_user.language, db_user=db_user, scheduled_pct=_scheduled, back_callback=periods_back
            ),
            parse_mode='HTML',
        )

    await state.update_data(selected_tariff_id=tariff_id)
```

Then in `select_tariff`, replace ONLY the non-daily `else:` body (currently ~743-808) with a call to the helper. The resulting structure:
```python
    is_daily = getattr(tariff, 'is_daily', False)

    if is_daily:
        # ... (UNCHANGED daily block, lines ~675-742) ...
    else:
        await _render_tariff_entry(callback, db_user, db, state, tariff)

    await state.update_data(selected_tariff_id=tariff_id)
    await callback.answer()
```
(Keep the daily block exactly as it is; only the non-daily `else` body is replaced by the helper call. The trailing `await state.update_data(...)` and `await callback.answer()` already exist at the end of `select_tariff` — do not duplicate them.)

- [ ] **Step 2: Skip the list in `show_tariffs_list`**

In `show_tariffs_list`, after `purchased_tariff_ids` is computed (~620) and immediately before building `tariffs_text` (~632), add a single-tariff short-circuit:

```python
    # Один активный тариф — пропускаем экран выбора, ведём сразу в его flow.
    if len(tariffs) == 1:
        only = tariffs[0]
        if only.id not in purchased_tariff_ids:
            await state.update_data(single_tariff=True, selected_tariff_id=only.id)
            if getattr(only, 'is_daily', False):
                # daily обрабатывает select_tariff целиком
                callback.data = f'nz!_tariff_select:{only.id}'
                await select_tariff(callback, db_user, db, state)
                return
            await _render_tariff_entry(callback, db_user, db, state, only)
            await callback.answer()
            return
```

- [ ] **Step 3: Verify import**

Run: `.venv\Scripts\python.exe -c "import app.handlers.subscription.tariff_purchase; print('import OK')"`
Expected: `import OK`.

- [ ] **Step 4: Manual verification**

Per the run skill: with the live bot (only the "Безлимит" tariff active), open the buy menu → it should go straight to the period/confirm screen (no "Выберите тариф"). On the confirm screen, −/＋ adjusts devices and the total updates (1=30₽, 3=70₽, 5=100₽ for a 30-day period). "Назад" from the period screen returns to the main menu.

- [ ] **Step 5: Commit**

```
git add app/handlers/subscription/tariff_purchase.py
git commit -m "feat(purchase): skip tariff list when a single tariff is active"
```

---

## Task 7: Regression + end-to-end

- [ ] **Step 1: Targeted test run**

Run: `.venv\Scripts\python.exe -m pytest tests/handlers/test_tariff_device_options.py tests/services/test_pricing_engine_device_tiers.py tests/cabinet/subscription -q`
Expected: all pass (new helper tests + device-pricing + cabinet subscription suites).

- [ ] **Step 2: Import sanity**

Run: `.venv\Scripts\python.exe -c "import app.handlers.subscription.tariff_purchase, app.handlers.subscription.purchase; print('import OK')"`
Expected: `import OK`.

- [ ] **Step 3: End-to-end (manual)**

1. Single active tariff "Безлимит": buy menu → straight to confirm (no list). −/＋ devices: 1→30₽, 2→50₽, 3→70₽, 4→85₽, 5→100₽ (30-day). Buy with 3 → subscription created with `device_limit=3`, charged 70₽.
2. Two+ active tariffs (re-enable another): buy menu shows the list again.
3. After purchase, "Изменение устройств" still works (tiered pricing, shipped earlier).

- [ ] **Step 4: Final commit (if tweaks)**

```
git add -A
git commit -m "test: purchase device-count regression pass"
```

---

## Self-review notes

- **Spec coverage:** single-tariff skip (T6 + back_callback T2), device −/＋ on confirm (T2 keyboard, T3 preview, T4 handler), price via engine (T3, T5), chosen device_limit charged + persisted to subscription (T5), `devices_selectable` gate + clamp (T1, T3, T4), tests (T1, T7). All spec sections mapped.
- **Out of scope (per spec):** daily tariff confirm (kept in `select_tariff`, no −/＋), custom-days/custom-traffic device pick, renewal device pick (extend branches unchanged).
- **Type consistency:** `_tariff_device_purchase_options` returns `(selectable, base, effective_max)` and is used identically in T3/T4/T5. State key `selected_device_limit` consistent across T3/T4/T5. `back_callback` param consistent across the three keyboard helpers. Callback `nz!_tariff_dev:{id}:{period}:{N}` parsed in T4 matches the format emitted in T2.
- **Known nuance:** `select_tariff_period` preview now prices via the engine (was a manual `_apply_promo_discount`); this makes preview == charge (the charge already used the engine). If a scheduled-promo discount was previously shown in preview but not applied at charge, the screens now agree on the engine's number — intentional consistency, not a regression of the charge.
