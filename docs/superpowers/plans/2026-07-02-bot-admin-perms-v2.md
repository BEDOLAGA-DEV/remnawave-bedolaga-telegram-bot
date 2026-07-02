# Bot-Admin Permissions v2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Fix 3 issues found testing the v1 section-permission feature: (#2) broken admin pagination, (#3) Триалы/Цены/Отзывы/Акции not independently toggleable, (#1) submenu buttons shown without child access — plus close the `nz!_`/`spromo_` gating hole (admin actions in those namespaces bypass the section middleware).

**Architecture:** The v1 middleware gates only callbacks literally starting with `admin_`. This plan (a) makes gating **resolution-driven** (gate any callback that `resolve_admin_section` maps to a section; pass everything that maps to None) so `nz!_`/`spromo_` admin actions can be gated without denying the many `nz!_` USER callbacks; (b) adds 4 dedicated sections; (c) filters submenu buttons by their child sections; (d) fixes the pagination-prefix mismatch. Every map change carries a mandatory per-token 0-regression proof.

**Tech Stack:** Python 3.13, aiogram 3. Tests DB-free (mock sessions; `.venv/Scripts/python.exe -m pytest <path> -v`; async tests need no marker). Patch settings via `type(settings)` (pydantic instance). Design inputs: the D1–D6 inventory (this session); full nz! per-token classification saved at `<session>/tool-results/b265h1slg.txt`.

**Valid sections after this plan (14):** users, payments, tariffs, subscriptions, promos, broadcasts, servers, support, settings, analytics, **trials, pricing, reviews, offers**.

---

## Task 1: Fix admin pagination prefix mismatch (#2)

**Root cause:** `get_admin_pagination_keyboard` ([app/keyboards/admin.py:2187](../../../app/keyboards/admin.py)) emits `callback_data=f'nz!_{callback_prefix}_page_{n}'`, but 8 of its 9 call sites have handlers registered on `<prefix>_page_` WITHOUT the `nz!_`. Only `promo_group_members` pagination was updated to expect `nz!_`. Minimal fix: make the helper emit WITHOUT `nz!_`, and update the one handler (promo_groups) that currently expects `nz!_`.

**Files:**
- Modify: `app/keyboards/admin.py:2197,2202` (drop `nz!_` from emitted page callbacks)
- Modify: `app/handlers/admin/promo_groups.py:1386` (regex) and `:553` (first-page emit) → drop `nz!_` to match
- Test: `tests/keyboards/test_admin_pagination.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/keyboards/test_admin_pagination.py`:

```python
"""get_admin_pagination_keyboard must emit callbacks the admin list handlers listen for."""
from app.keyboards.admin import get_admin_pagination_keyboard


def _cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_page_callbacks_have_no_nz_prefix():
    kb = get_admin_pagination_keyboard(
        current_page=2, total_pages=5, callback_prefix='admin_campaigns_list',
        back_callback='admin_campaigns', language='ru',
    )
    cbs = _cbs(kb)
    assert 'admin_campaigns_list_page_1' in cbs   # prev
    assert 'admin_campaigns_list_page_3' in cbs   # next
    assert not any(c.startswith('nz!_admin_campaigns_list_page_') for c in cbs)
```

- [ ] **Step 2: Run test → FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/keyboards/test_admin_pagination.py -v`
Expected: FAIL — current emit is `nz!_admin_campaigns_list_page_*`.

- [ ] **Step 3: Fix the helper**

In `app/keyboards/admin.py`, lines ~2197 and ~2202, change:
```python
callback_data=f'nz!_{callback_prefix}_page_{current_page - 1}'
...
callback_data=f'nz!_{callback_prefix}_page_{current_page + 1}'
```
to (drop `nz!_`):
```python
callback_data=f'{callback_prefix}_page_{current_page - 1}'
...
callback_data=f'{callback_prefix}_page_{current_page + 1}'
```
Leave the middle `nz!_current_page` label button unchanged (it's a no-op page indicator).

- [ ] **Step 4: Fix the one handler that expected `nz!_`**

In `app/handlers/admin/promo_groups.py`:
- line ~1386: change the registration regex `r'^nz\!_promo_group_members_\d+_page_\d+$'` → `r'^promo_group_members_\d+_page_\d+$'`.
- line ~553: change the emitted first-page callback `f'nz!_promo_group_members_{...}_page_1'` → `f'promo_group_members_{...}_page_1'`.
Read both lines first to get the exact surrounding code; only remove the `nz!_` token.

- [ ] **Step 5: Verify all pagination call sites now match their handlers**

Read each pair and confirm the emitted `<prefix>_page_` now matches the handler's `startswith`/regex (from the D1 table): `admin_users_list`, `admin_users_balance_list`, `admin_users_campaign_list`, `admin_users_potential_customers_list`, `admin_users_ready_to_renew_list` ([users.py](../../../app/handlers/admin/users.py) registrations ~6818-6833), `admin_campaigns_list` ([campaigns.py:1836](../../../app/handlers/admin/campaigns.py)), `admin_contests_list`/`admin_contest_detailed_stats` ([contests.py:1425,1431](../../../app/handlers/admin/contests.py)), `admin_promo_list` ([promocodes.py:1096](../../../app/handlers/admin/promocodes.py)), `promo_group_members` ([promo_groups.py:1386](../../../app/handlers/admin/promo_groups.py)). Do NOT edit those handlers (they already expect the non-`nz!_` form) — just confirm.

- [ ] **Step 6: Run test → PASS**

Run: `.venv/Scripts/python.exe -m pytest tests/keyboards/test_admin_pagination.py -v` → PASS.
Import check: `.venv/Scripts/python.exe -c "import app.keyboards.admin, app.handlers.admin.promo_groups; print('ok')"`.

- [ ] **Step 7: Commit**

```bash
git add tests/keyboards/test_admin_pagination.py app/keyboards/admin.py app/handlers/admin/promo_groups.py
git commit -m "fix(admin): admin pagination emits handler-matching page callbacks (drop stray nz! prefix)"
```

---

## Task 2: Add dedicated sections trials/pricing/reviews/offers (#3)

Split 4 features out of their bundled sections into their own toggles.

**Files:**
- Modify: `app/database/crud/bot_role.py:15-26` (`BOT_ROLE_SECTIONS` +4)
- Modify: `app/handlers/admin/bot_roles.py:17-28` (`SECTION_LABELS` +4)
- Modify: `app/middlewares/admin_permission.py` (`ADMIN_CALLBACK_SECTION_MAP` — re-map the feature prefixes to the new sections)
- Test: `tests/middlewares/test_admin_sections_v2.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/middlewares/test_admin_sections_v2.py`:

```python
"""Trials/pricing/reviews/offers resolve to their own sections, not the bundled ones."""
from app.database.crud.bot_role import BOT_ROLE_SECTIONS
from app.middlewares.admin_permission import resolve_admin_section as r


def test_new_sections_registered():
    for s in ('trials', 'pricing', 'reviews', 'offers'):
        assert s in BOT_ROLE_SECTIONS


def test_features_map_to_new_sections():
    assert r('admin_trials') == 'trials'
    assert r('admin_trials_reset') == 'trials'
    assert r('admin_pricing') == 'pricing'
    assert r('admin_pricing_edit:5') == 'pricing'
    assert r('admin_subs_pricing') == 'pricing'          # alias, must win over admin_subs_
    assert r('admin_reviews') == 'reviews'
    assert r('admin_review_approve_5') == 'reviews'
    assert r('admin_scheduled_promos') == 'offers'
    assert r('spromo_view:3') == 'offers'                # sub-actions now gated
    assert r('spromo_delete_confirm:3') == 'offers'


def test_labels_present():
    from app.handlers.admin.bot_roles import SECTION_LABELS
    for s in ('trials', 'pricing', 'reviews', 'offers'):
        assert s in SECTION_LABELS
```

- [ ] **Step 2: Run → FAIL** (`.venv/Scripts/python.exe -m pytest tests/middlewares/test_admin_sections_v2.py -v`).

- [ ] **Step 3: Add the sections + labels**

`app/database/crud/bot_role.py` — append to `BOT_ROLE_SECTIONS`:
```python
    'trials',
    'pricing',
    'reviews',
    'offers',
```
`app/handlers/admin/bot_roles.py` `SECTION_LABELS` — add:
```python
    'trials': '🎁 Триалы',
    'pricing': '💲 Цены',
    'reviews': '⭐ Отзывы',
    'offers': '🔥 Акции',
```

- [ ] **Step 4: Re-map the feature prefixes**

In `ADMIN_CALLBACK_SECTION_MAP`, add a NEW group placed **before** the existing `payments`/`promos`/`subscriptions` groups so the more-specific new entries win (first-match order). Add:
```python
    # trials (was payments)
    ('admin_trials', 'trials'),
    ('admin_trials_reset', 'trials'),
    # pricing (was payments; admin_subs_pricing was subscriptions)
    ('admin_subs_pricing', 'pricing'),   # must precede admin_subs_
    ('admin_pricing', 'pricing'),
    # reviews (was promos)
    ('admin_reviews', 'reviews'),
    ('admin_review_', 'reviews'),
    # offers (was promos; spromo_* was ungated)
    ('admin_scheduled_promos', 'offers'),
    ('spromo_', 'offers'),
```
Then REMOVE the now-superseded old entries if they would otherwise still match first (e.g. if `admin_trials`/`admin_pricing`/`admin_reviews`/`admin_review_`/`admin_scheduled_promos` currently sit in payments/promos groups earlier in the map, either delete those old tuples or ensure the new group precedes them). Read the current map fully and make the ordering deterministic: for each of `admin_trials`, `admin_pricing`, `admin_subs_pricing`, `admin_reviews`, `admin_review_`, `admin_scheduled_promos`, the FIRST matching tuple must be the new-section one.

> Note: `spromo_` does not start with `admin_`; it will only resolve once Task 3 makes the middleware resolution-driven. For now `resolve_admin_section('spromo_view:3')` must still return `'offers'` (the resolver itself must match `spromo_` — see Step 5).

- [ ] **Step 5: Ensure the resolver matches `spromo_` and `admin_subs_pricing` ordering**

Read `resolve_admin_section` ([admin_permission.py:164-177](../../../app/middlewares/admin_permission.py)). If it has a `startswith('admin_')` precondition that would skip `spromo_`, widen it now to also allow `spromo_` (full nz! widening is Task 3). Minimal: change the precondition so `spromo_`-prefixed callbacks are looked up in the map too. Confirm `admin_subs_pricing` resolves to `pricing` (its tuple precedes `admin_subs_`).

- [ ] **Step 6: 0-regression proof (mandatory)**

Write a throwaway script (scratchpad) that extracts all registered `admin_*` and `spromo_*` callbacks from `app/handlers/admin/**` and `app/keyboards/**`, builds the PRE resolver (`git show HEAD:app/middlewares/admin_permission.py`) and POST (current), and for each callback compares old vs new. REQUIRE: the ONLY changes are (payments→trials/pricing), (promos→reviews/offers), (subscriptions→pricing for `admin_subs_pricing`), and (None→offers for `spromo_*`). No other token may change section. Print the full change list; if anything unexpected changed, fix ordering.

- [ ] **Step 7: Run → PASS** (`tests/middlewares/test_admin_sections_v2.py` + `tests/middlewares/` all green). Import: `.venv/Scripts/python.exe -c "import app.bot; print('ok')"`.

- [ ] **Step 8: Commit**

```bash
git add tests/middlewares/test_admin_sections_v2.py app/database/crud/bot_role.py app/handlers/admin/bot_roles.py app/middlewares/admin_permission.py
git commit -m "feat(admin): dedicated trials/pricing/reviews/offers sections"
```

---

## Task 3: Gate `nz!_` and `spromo_` admin actions (close the hole)

Make the middleware resolution-driven and add the `nz!_` ADMIN prefixes to the map — WITHOUT gating any `nz!_` USER callback.

**Files:**
- Modify: `app/middlewares/admin_permission.py` (resolver widening + map additions + middleware guard change)
- Test: `tests/middlewares/test_nz_gating.py` (create)

**Design input:** full nz! classification at `<session>/tool-results/b265h1slg.txt` (120 ADMIN, 6 AMBIGUOUS, 312 USER). The ADMIN prefixes are grouped in D3(a). **Exclude the ambiguous `nz!_period_`** (used by USER period selection AND admin revenue-by-period — gating it breaks user purchase). Rely on the admin handler's own `@admin_required` for that one.

- [ ] **Step 1: Write the failing test**

Create `tests/middlewares/test_nz_gating.py`:

```python
"""nz!_/spromo_ ADMIN actions gate; nz!_ USER actions never gate."""
from app.middlewares.admin_permission import resolve_admin_section as r

ADMIN_SAMPLES = {
    'nz!_broadcast_all': 'broadcasts',
    'nz!_criteria_today': 'broadcasts',
    'nz!_sync_all_users': 'servers',
    'nz!_node_restart_5': 'servers',
    'nz!_squad_delete_3': 'servers',
    'nz!_promo_delete_7': 'promos',
    'nz!_promo_group_manage_2': 'promos',
    'nz!_promo_offer_edit_1': 'promos',
    'nz!_poll_create': 'promos',
    'nz!_maintenance_panel': 'settings',
    'nz!_welcome_text_panel': 'settings',
    'nz!_reqch:list': 'settings',
    'nz!_user_messages_panel': 'broadcasts',
    'spromo_view:3': 'offers',
}

USER_SAMPLES = [
    'nz!_menu_buy', 'nz!_trial_activate', 'nz!_rules_accept', 'nz!_back_to_menu',
    'nz!_current_page', 'nz!_noop', 'nz!_language_select:ru', 'nz!_subscription_connect',
    'nz!_promo_sub', 'nz!_menu_promocode', 'nz!_poll_answer_1', 'nz!_period_30',
    'nz!_my_tickets', 'nz!_incy_open', 'nz!_bio_reward_open',
]


def test_admin_nz_callbacks_gate():
    for cb, sect in ADMIN_SAMPLES.items():
        assert r(cb) == sect, f'{cb} -> {r(cb)} (want {sect})'


def test_user_nz_callbacks_never_gate():
    for cb in USER_SAMPLES:
        assert r(cb) is None, f'{cb} wrongly resolved to {r(cb)}'
```

Adjust the exact ADMIN sample strings to real ones from the classification file if any differ; keep the invariant: every USER sample must resolve to None.

- [ ] **Step 2: Run → FAIL** (nz! all resolve None today).

- [ ] **Step 3: Widen the resolver**

In `resolve_admin_section`, remove/replace any `startswith('admin_')` precondition so the map lookup runs for `admin_`, `nz!_`, and `spromo_` prefixes alike (a plain "walk the map, first match wins" over the raw callback string is simplest). Verify `admin_*` and `spromo_*` behavior is unchanged from Task 2.

- [ ] **Step 4: Add nz! ADMIN prefixes to the map**

Add the D3(a) ADMIN prefixes, grouped by section, using SPECIFIC prefixes (never a broad `nz!_promo_` that would swallow USER `nz!_promo_sub`). Minimum set (from the inventory; expand from the classification file, but each must pass Step 6):
```python
    # broadcasts
    ('nz!_broadcast_', 'broadcasts'), ('nz!_criteria_', 'broadcasts'),
    ('nz!_bcast_', 'broadcasts'), ('nz!_edit_buttons', 'broadcasts'),
    ('nz!_buttons_confirm', 'broadcasts'), ('nz!_btn_', 'broadcasts'),
    ('nz!_add_media_', 'broadcasts'), ('nz!_change_media', 'broadcasts'),
    ('nz!_confirm_media', 'broadcasts'), ('nz!_replace_media', 'broadcasts'),
    ('nz!_skip_media', 'broadcasts'),
    ('nz!_user_messages_panel', 'broadcasts'), ('nz!_add_user_message', 'broadcasts'),
    ('nz!_edit_user_message', 'broadcasts'), ('nz!_delete_user_message', 'broadcasts'),
    ('nz!_toggle_user_message', 'broadcasts'), ('nz!_view_user_message', 'broadcasts'),
    ('nz!_list_user_messages', 'broadcasts'), ('nz!_user_messages_stats', 'broadcasts'),
    # settings
    ('nz!_welcome_text_panel', 'settings'), ('nz!_edit_welcome_text', 'settings'),
    ('nz!_preview_welcome_text', 'settings'), ('nz!_reset_welcome_text', 'settings'),
    ('nz!_toggle_welcome_text', 'settings'), ('nz!_show_welcome_text', 'settings'),
    ('nz!_show_formatting_help', 'settings'), ('nz!_show_placeholders_help', 'settings'),
    ('nz!_maintenance_', 'settings'), ('nz!_manual_notify_', 'settings'),
    ('nz!_reqch', 'settings'),
    # servers
    ('nz!_node_', 'servers'), ('nz!_squad_', 'servers'), ('nz!_sqd_', 'servers'),
    ('nz!_create_squad_finish', 'servers'), ('nz!_create_tgl_', 'servers'),
    ('nz!_cancel_squad_create', 'servers'), ('nz!_cancel_rename_', 'servers'),
    ('nz!_sync_', 'servers'), ('nz!_remnawave_auto_sync', 'servers'),
    ('nz!_force_cleanup_orphaned', 'servers'),
    # promos (SPECIFIC — never bare nz!_promo_)
    ('nz!_promo_manage_', 'promos'), ('nz!_promo_toggle_', 'promos'),
    ('nz!_promo_stats_', 'promos'), ('nz!_promo_delete_', 'promos'),
    ('nz!_promo_edit_', 'promos'), ('nz!_promo_type_', 'promos'),
    ('nz!_promo_select_group_', 'promos'),
    ('nz!_promo_group_', 'promos'), ('nz!_promo_offer_', 'promos'),
    ('nz!_poll_create', 'promos'), ('nz!_poll_view', 'promos'), ('nz!_poll_stats', 'promos'),
    ('nz!_poll_send', 'promos'), ('nz!_poll_delete', 'promos'), ('nz!_poll_target', 'promos'),
    ('nz!_poll_custom_target', 'promos'), ('nz!_poll_custom_menu', 'promos'),
    # tariffs
    ('nz!_tariff_type_daily', 'tariffs'), ('nz!_tariff_type_periodic', 'tariffs'),
```
Do NOT add `nz!_period_`, `nz!_poll_answer`, `nz!_poll_start`, `nz!_promo_sub`, `nz!_menu_promocode`, `nz!_review*`, `nz!_ticket*`, or any USER prefix.

- [ ] **Step 5: Make the middleware resolution-driven**

In `AdminPermissionMiddleware.__call__` ([admin_permission.py:192-197](../../../app/middlewares/admin_permission.py)), replace the `if not cb.startswith('admin_'): return handler` guard with:
```python
        cb = event.data or ''
        required = resolve_admin_section(cb)
        if required is None:
            return await handler(event, data)   # not a section-gated callback
```
Keep the `ALWAYS_ALLOWED_PREFIXES` early-return and the superadmin bypass and the role/section check that follow. Keep the existing `logger.info` for unmapped `admin_*` only if it still makes sense; otherwise drop it (a None result is now the normal pass path for all USER callbacks and would flood logs — REMOVE the blanket unmapped-log, or guard it to `cb.startswith('admin_')`).

- [ ] **Step 6: 0-regression proof over ALL nz! tokens (mandatory, security-critical)**

Throwaway script: extract every distinct `nz!_*` and `spromo_*` token from `app/`; build PRE (`git show HEAD:...admin_permission.py`) vs POST resolvers; for each token print old→new. REQUIRE:
- Every token classified USER in `<session>/tool-results/b265h1slg.txt` resolves to **None** post-change (print any that don't — those are production-breaking over-matches; make the offending prefix more specific until zero).
- `nz!_period_` resolves to None.
- The set that flips None→section equals the intended ADMIN set only.
Also re-run Task 2's `admin_*` proof to confirm no admin_* token changed unexpectedly.

- [ ] **Step 7: Non-admin USER trace test**

Add to `tests/middlewares/test_nz_gating.py` a middleware-level test: a plain user (no BotAdminRole, not superadmin) clicking `nz!_menu_buy` → handler IS called (not denied). Use the mock pattern from `tests/middlewares/test_admin_permission.py` (`not_superadmin` fixture, patch `BotRoleCRUD.get_bot_role` returning None, `db_user` a normal user). Assert `resolve_admin_section` None-path passes it through.

- [ ] **Step 8: Run → PASS** (`tests/middlewares/` all green). Import `app.bot`.

- [ ] **Step 9: Commit**

```bash
git add tests/middlewares/test_nz_gating.py app/middlewares/admin_permission.py
git commit -m "feat(admin): gate nz!/spromo admin actions via resolution-driven middleware"
```

---

## Task 4: Filter submenu buttons by child sections (#1)

Hide a `admin_submenu_*` button when the admin has none of that submenu's child sections.

**Files:**
- Modify: `app/keyboards/admin.py` (extend `filter_admin_keyboard` with a submenu→sections map)
- Test: `tests/keyboards/test_admin_keyboard_filter.py` (extend)

- [ ] **Step 1: Compute the authoritative submenu→sections map**

Because Tasks 2–3 changed the section map, RE-RUN the resolver over each submenu keyboard's children to get current section sets (do not trust the pre-change D5 table). Throwaway script: for each of `get_admin_users_submenu_keyboard`, `_promo_`, `_communications_`, `_support_`, `_settings_`, `_system_`, collect child callbacks and `resolve_admin_section` each; print the distinct non-None set per submenu. Use the result as `_SUBMENU_SECTIONS` below.

- [ ] **Step 2: Write the failing test**

Extend `tests/keyboards/test_admin_keyboard_filter.py`:

```python
def test_submenu_hidden_when_no_child_sections():
    from app.keyboards.admin import get_admin_main_keyboard, filter_admin_keyboard
    kb = get_admin_main_keyboard('ru')
    # admin with only 'servers' — the Users/Подписки submenu (users/subscriptions) must be hidden
    filtered = filter_admin_keyboard(kb, permissions=['servers'], is_super=False)
    cbs = {b.callback_data for row in filtered.inline_keyboard for b in row}
    assert 'admin_submenu_users' not in cbs
    # a settings-submenu contains servers -> an admin with 'servers' SHOULD still see it
    assert 'admin_submenu_settings' in cbs


def test_submenu_shown_when_has_a_child_section():
    from app.keyboards.admin import get_admin_main_keyboard, filter_admin_keyboard
    kb = get_admin_main_keyboard('ru')
    filtered = filter_admin_keyboard(kb, permissions=['users'], is_super=False)
    cbs = {b.callback_data for row in filtered.inline_keyboard for b in row}
    assert 'admin_submenu_users' in cbs
```

Adjust the expected submenu membership to the Step-1 computed map (e.g. if `admin_submenu_settings` contains `servers`, the first test's `assert ... in cbs` holds; otherwise pick a submenu that does/doesn't contain `servers` accordingly).

- [ ] **Step 3: Run → FAIL** (submenu buttons currently always kept).

- [ ] **Step 4: Extend the filter**

In `app/keyboards/admin.py`, add above `filter_admin_keyboard`:
```python
# Sections reachable inside each admin submenu (recomputed from the section map).
_SUBMENU_SECTIONS: dict[str, set[str]] = {
    'admin_submenu_users': {...},          # from Step 1
    'admin_submenu_promo': {...},
    'admin_submenu_communications': {...},
    'admin_submenu_support': {...},
    'admin_submenu_settings': {...},
    'admin_submenu_system': {...},
}
```
In `filter_admin_keyboard`, before the generic keep-logic, special-case submenu buttons:
```python
            if cb in _SUBMENU_SECTIONS:
                if allowed & _SUBMENU_SECTIONS[cb]:
                    kept.append(button)
                continue
```
(Superadmin still short-circuits at the top and sees everything.)

- [ ] **Step 5: Run → PASS** (`tests/keyboards/` all green). Import `app.handlers.admin.main`.

- [ ] **Step 6: Commit**

```bash
git add tests/keyboards/test_admin_keyboard_filter.py app/keyboards/admin.py
git commit -m "feat(admin): hide submenu buttons when admin lacks all their child sections"
```

---

## Task 5: Full verification

- [ ] **Step 1** Run the whole v2 suite:
```
.venv/Scripts/python.exe -m pytest tests/keyboards/ tests/middlewares/ tests/handlers/admin/test_bot_roles_fsm.py tests/utils/test_super_admin_required.py tests/database/test_bot_role_crud.py -q
```
Expected: all PASS.

- [ ] **Step 2** Import smoke: `.venv/Scripts/python.exe -c "import app.bot, app.handlers.admin.main, app.handlers.admin.bot_roles, app.keyboards.admin, app.middlewares.admin_permission; print('ok')"`.

- [ ] **Step 3** Manual acceptance (running bot, super-admin + test user):
  1. Grant test user only `servers` → main menu shows Серверы + Settings-submenu (contains servers) but NOT Users/Подписки submenu, NOT Триалы/Цены/Отзывы/Акции.
  2. Grant `trials` → Триалы button visible & works; Цены hidden; refund/sync still denied.
  3. Grant `promos` but NOT `offers` → Акции (scheduled promos) denied; promocodes work.
  4. Open any admin list (campaigns/users/promocodes) → pagination ⬅️/➡️ works.
  5. As a normal (non-admin) user: menu/buy/trial/tickets all work (no ACCESS_DENIED from nz! gating).

---

## Self-Review

**Coverage:** #2→Task 1; #3→Task 2; nz!/spromo hole→Task 3; #1→Task 4; verify→Task 5. ✓
**Placeholders:** `{...}` in Task 4 `_SUBMENU_SECTIONS` and Step-1 map are computed values the implementer fills from the resolver run — flagged as such, not left vague. ✓
**Security proofs:** Task 2 Step 6 and Task 3 Step 6 both mandate per-token before/after 0-regression proofs; Task 3 specifically requires every USER `nz!_` token to stay None (production-safety). ✓
**Ordering hazards:** `admin_subs_pricing` before `admin_subs_` (Task 2); specific `nz!_promo_*` never broad `nz!_promo_` (Task 3); `nz!_period_` excluded. ✓
