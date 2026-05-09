# Brand Review — `app/localization/locales/ru.json`

Generic content review (no formal brand guidelines provided). Scope: all 1,792 user-facing Russian strings. Method: programmatic scans for mechanical issues + manual semantic pass + cross-reference to `en.json` on high-traffic flows.

## Summary

**Overall grade: B−.** Voice is consistent, mostly polite, and HTML/placeholders are clean. The two biggest drags on quality are (a) systemic ё/е inconsistency across many high-visibility words and (b) tone leak — two key referral strings switch to «ты» inside an otherwise «вы» cohort, which the reader will read as the bot suddenly shifting register.

**Top priorities:**
1. Normalize «ты»/«вы» — lock user-facing copy to «вы» (admin copy can stay neutral). Two REFERRAL keys are the obvious offenders.
2. Adopt one ё policy and apply it. ~30+ strings mix forms (автоплатеж/автоплатёж, запрещен/запрещён, удален/удалён, etc.).
3. Make ~60 bare error strings actionable — every error needs a what-now next step, even one short clause.

## Findings

### High severity

| # | Issue | Key(s) | Suggestion |
|---|-------|--------|------------|
| H1 | Pronoun register switches to «ты» inside REFERRAL cohort that elsewhere uses «вы» | `REFERRAL_INVITE_BONUS`, `REFERRAL_INVITE_MESSAGE` | Convert «тебя/тебе/ты» → «вас/вам/вы»; align with `REFERRAL_CODE_QUESTION`, `REFERRAL_INFO`, etc. |
| H2 | Bare error: «❌ Доступ запрещен» — no reason, no next step, plus missing ё | `ACCESS_DENIED` | «❌ <b>Доступ запрещён.</b> Если это ошибка, обратитесь в поддержку.» |
| H3 | Stand-alone «Ошибка» / «Не удалось …» strings with no recovery hint (~60 keys) | `ADMIN_PAYMENT_STATUS_FAILED`, `DEVICE_FETCH_ERROR`, `DEVICE_LIST_FETCH_ERROR`, `DEVICE_PAGE_LOAD_ERROR`, `SAVED_CARDS_UNLINK_ERROR`, `ADMIN_PAYMENT_CHECK_FAILED`, `ADMIN_USER_PROMO_GROUP_ERROR`, `COUNTRY_CHANGES_NOT_FOUND`, … | Append one of: «Попробуйте позже», «Обратитесь в поддержку», «Проверьте подключение». Errors should always answer «что теперь?». |
| H4 | Unbalanced HTML — `<blockquote>` opens in TITLE, closes in FOOTER. Two parsers will disagree if templates ever render TITLE in isolation | `SUBSCRIPTION_CONNECTED_DEVICES_TITLE` (open), `SUBSCRIPTION_CONNECTED_DEVICES_FOOTER` (close) | Either keep tags within one string, or comment in code that these two MUST be concatenated. Add a rendering test. |
| H5 | Missing ё on participle/adjective forms used widely across UI | `AUTOPAY_FAILED`, `AUTOPAY_MENU_TEXT`, `AUTOPAY_SUCCESS`, `AUTOPAY_TOGGLE_SUCCESS`, `SUBSCRIPTION_INFO`, `SUBSCRIPTION_EXPIRING_PAID`, `SAVED_CARDS_CONFIRM_UNLINK`, `SAVED_CARDS_LAST_CARD_WARNING` | автоплатеж → автоплатёж (8 strings inconsistent vs `AUTOPAY_BUTTON`/`RECURRENT_TOPUP_*` already using ё). |

### Medium severity

| # | Issue | Key(s) / Stats | Suggestion |
|---|-------|---------------|------------|
| M1 | «партнер» everywhere, «партнёр» nowhere (4 vs 0) | `ADMIN_BROADCAST_BUTTON_REFERRALS`, `ADMIN_REFERRALS`, `ADMIN_STATS_REFERRALS`, `MENU_REFERRALS` | Either drop ё globally as policy, or fix to «Партнёрка». Currently silent inconsistency with the rest of the file. |
| M2 | «ещё» 18× vs «еще» 13× | scattered | Pick one. Recommend «ещё» (matches majority + ё-policy). |
| M3 | «звезд» 1× vs «звёзд» 0× — likely should be «звёзд» when referring to Telegram Stars | search `звезд` | Change to «звёзд» if ё-on policy. |
| M4 | «зачислен» 3× vs «зачислён» 0× | e.g. `HELEKET_PAYMENT_SUCCESS` | Add ё. |
| M5 | «удален»/«включен»/«отключен» 1+3+3× alongside «удалён»/«включён»/«отключён» 3+3+3× | 18 keys total | Same ё policy decision — apply once and lint. |
| M6 | Two terms for same concept: «Партнерка» (4 keys) vs «Реферал…» (37 keys) | menu shows «Партнерка», content uses «реферальная программа/реферал» | Pick one umbrella term. Recommend keeping «Реферальная программа» as the page title and dropping «Партнерка» from menu — internally consistent + clearer to first-time users. |
| M7 | Three-dot ellipsis `...` (10 keys) vs typographic `…` (1 key) | `LOADING`, `REGISTRATION_COMPLETING`, `ADMIN_PINNED_SAVING`, `ADMIN_PROMO_OFFER_SENDING`, `ADMIN_SQUAD_MIGRATION_IN_PROGRESS`, … | Replace `...` → `…` everywhere. One pass, no risk. |
| M8 | 49 strings have trailing whitespace | `ADMIN_PANEL`, `ADMIN_CONTESTS_LIST_HEADER`, `CONTEST_WIN`, `ADMIN_COMMUNICATIONS_SUBMENU_TITLE`, `ADMIN_PROMO_SUBMENU_TITLE`, … | Strip; add lint check. Trailing whitespace makes copy harder to maintain and can show up at end of bot messages. |
| M9 | 104 distinct strings duplicated across keys (same value, different key) | e.g. `ADMIN_BROADCAST_BUTTON_REFERRALS` = `ADMIN_REFERRALS` = `ADMIN_STATS_REFERRALS` = `MENU_REFERRALS` = «🤝 Партнерка» | Consolidate where flows share the same label. Reduces drift; today, fixing «Партнерка» → «Партнёрка» means editing 4 keys, not 1. |
| M10 | Long button labels likely to wrap awkwardly on mobile | `SUBSCRIPTION_HAPP_OPEN_BUTTON_HINT` (92 chars), `PAL24_CARD_PAY_BUTTON` (40), `MULENPAY_PAY_BUTTON` (32), `PAL24_SBP_PAY_BUTTON` (32) | Shorten to ≤24 chars where possible. `SUBSCRIPTION_HAPP_OPEN_BUTTON_HINT` is mislabelled — its content is a sentence, not a button. Verify call site. |
| M11 | «Запрещено» in `RULES_TEXT_DEFAULT` opens a list of don'ts written as imperative «не …» elsewhere — mixed list voice | `RULES_TEXT_DEFAULT` | Choose one rule grammar (active prohibitions «Не делайте X» **or** noun phrases «Запрещено X») and apply to all rules. |
| M12 | `ADMIN_FAQ_HTML_ERROR` / `ADMIN_PRIVACY_POLICY_HTML_ERROR` / `ADMIN_PUBLIC_OFFER_HTML_ERROR` are identical but split into 3 keys | as listed | If the admin needs source context, surface that via key naming, not duplicated copy. Or consolidate to one error with `{section}` placeholder. |
| M13 | `ADMIN_INVALID_NUMBER` / `ADMIN_INVALID_JSON` say what failed but not what to do | as listed | «Некорректное число. Введите целое число.» / «Некорректный JSON. Проверьте синтаксис и повторите.» |
| M14 | Marketing claim in `WELCOME`: «быстрый и безопасный доступ к интернету без ограничений» | `WELCOME` | If unverified, soften «без ограничений» → «без блокировок региональных сервисов» or whatever the operator can substantiate. Generic «без ограничений» is a regulatory red flag in some jurisdictions. |

### Low severity

| # | Issue | Key(s) | Suggestion |
|---|-------|--------|------------|
| L1 | `CONTEST_BLITZ_BUTTON` ends with `!` — uncommon for a button label, but acceptable as energetic CTA | `CONTEST_BLITZ_BUTTON` | Optional: drop `!` for consistency with other buttons. |
| L2 | `ADMIN_PROMO_OFFER_PROMPT_BUTTON` value «Введите новый текст кнопки:» — looks like a prompt mis-keyed as `_BUTTON` | as listed | Verify call site; rename key or fix copy. |
| L3 | «ЮMoney», «СберPay» mix Cyrillic+Latin — these are official brand names, leave as-is | `PAYMENT_METHOD_YOO_MONEY`, `PAYMENT_METHOD_SBERBANK` | No change. Listed for transparency. |
| L4 | `ADMIN_CONTEST_MODE_REGISTERED` value starts with 3-codepoint compound emoji `🧑‍🤝‍🧑` | as listed | Acceptable; just flag — older Telegram clients render this poorly. |
| L5 | `❌` and `⚠️` used inconsistently for similar severities | many keys | Pick rule: `❌` = blocked/failed, `⚠️` = soft warning, `ℹ️` = info. Audit later. |

## Top Fixes (before/after)

### Fix 1 — `REFERRAL_INVITE_BONUS`
```diff
- 💎 При первом пополнении от {minimum} ты получишь {bonus} бонусом на баланс!
+ 💎 При первом пополнении от {minimum} вы получите {bonus} бонусом на баланс!
```

### Fix 2 — `REFERRAL_INVITE_MESSAGE`
```diff
- 🎯 <b>Приглашение в VPN сервис</b>
-
- Привет! Приглашаю тебя в отличный VPN сервис!
+ 🎯 <b>Приглашение в VPN-сервис</b>
+
+ Здравствуйте! Приглашаю вас в наш VPN-сервис.
```
Note: this string is sent *between users* (referrer → invitee). If product intent is informal peer-to-peer, the «ты» form is defensible — but then the rest of the REFERRAL cohort should match. Pick one register, document it.

### Fix 3 — `ACCESS_DENIED`
```diff
- ❌ Доступ запрещен
+ ❌ <b>Доступ запрещён.</b> Если это ошибка, обратитесь в поддержку.
```

### Fix 4 — `DEVICE_FETCH_ERROR` (template for ~60 bare errors)
```diff
- ❌ Ошибка получения устройств
+ ❌ Не удалось получить список устройств. Попробуйте позже или обратитесь в поддержку.
```

### Fix 5 — `AUTOPAY_SUCCESS`
```diff
- ✅ <b>Автоплатеж выполнен</b>
+ ✅ <b>Автоплатёж выполнен.</b>
```
Apply same ё correction to `AUTOPAY_FAILED`, `AUTOPAY_MENU_TEXT`, `AUTOPAY_TOGGLE_SUCCESS`, `AUTOPAY_ACTION_ENABLE`, `SAVED_CARDS_CONFIRM_UNLINK`, `SAVED_CARDS_LAST_CARD_WARNING`, `SUBSCRIPTION_INFO`, `SUBSCRIPTION_EXPIRING_PAID` (the eight known offenders).

### Fix 6 — Menu / `MENU_REFERRALS`
```diff
- 🤝 Партнерка
+ 🤝 Реферальная программа
```
(or, if ё-on: «🤝 Партнёрка»). Apply across `ADMIN_BROADCAST_BUTTON_REFERRALS`, `ADMIN_REFERRALS`, `ADMIN_STATS_REFERRALS`, `MENU_REFERRALS`.

### Fix 7 — Ellipsis sweep
Replace `...` → `…` in `LOADING`, `REGISTRATION_COMPLETING`, `ADMIN_PINNED_SAVING`, `ADMIN_PROMO_OFFER_SENDING`, `ADMIN_SQUAD_MIGRATION_IN_PROGRESS`, plus 5 others.

### Fix 8 — `RULES_TEXT_DEFAULT` (excerpt)
```diff
- 1. Запрещено использовать сервис для противоправной деятельности
- 2. Не распростр…
+ 1. Запрещено использовать сервис в противоправных целях.
+ 2. Запрещено распространять…
```
Make every numbered rule grammatically parallel and end-punctuate each rule.

## Compliance / Legal Flags

| Flag | Where | Action |
|------|-------|--------|
| Unsubstantiated claim «без ограничений» | `WELCOME` | Replace with substantiable wording or qualify. Generic «no limits» claims invite regulator/marketplace pushback. |
| Refund / payment failure messages omit support handle and operator name | `RECURRENT_TOPUP_FAILED`, `TRIAL_PAYMENT_FAILED`, `ADMIN_PAYMENT_CHECK_FAILED` | If operating in jurisdictions with consumer-protection rules (EU, RU 2300-1, etc.), spell out: who refunds, by what date, how the user contacts support. |
| `RULES_TEXT_DEFAULT` is the legal surface — currently a short list with no operator name, no jurisdiction, no contact, no price/refund clause | `RULES_TEXT_DEFAULT` | Have legal review the default. The bot ships with a placeholder ToS-equivalent visible to every user. |
| `WELCOME` and various marketing strings imply a service but don't disclose data handling | `WELCOME`, onboarding flow | Add link to privacy policy in onboarding (the admin keys exist: `ADMIN_PRIVACY_POLICY_*` — ensure the user-facing entry is wired). |

No PII or secrets in the locale file itself. No copy that paraphrases competitor wording verbatim.

## Terminology Inventory

| Concept | Variants found | Recommended |
|---------|---------------|-------------|
| Subscription | подписка (142), тариф (8) | подписка for the contract; тариф for the named plan inside it. Use «подписка» in CTAs. |
| Promo code | промокод (many), купон (0) | промокод. Already consistent — keep. |
| Referral | реферал (37), партнер (4 — only in menu label «Партнерка») | реферал/реферальная программа. Drop «партнёрка» everywhere or use only as marketing surface, not menu. |
| Balance | баланс (49), счёт (2) | баланс. Already consistent. |
| Devices | устройство (many) | устройство. Already consistent. |
| Cancel | отменить (12 forms), удалить (20 forms) | They're not synonyms — отменить = cancel a process; удалить = delete an entity. Spot-check that they're not swapped. |

## Recommended Next Steps

1. **Run a one-shot fix pass** on the 8 known автоплатёж keys, 4 «Партнерка» keys, the 2 REFERRAL «ты» keys, and `ACCESS_DENIED`. ~14 keys, 30 minutes, immediate UX uplift.
2. **Decide ё policy** (always-ё / always-no-ё), then sed-script the file. Add a CI check that fails on the wrong form. The bot already uses ё in some keys, so the path of least friction is **always ё**.
3. **Error-message audit** — go through the ~60 keys in the H3 list and append a next step. Worth ~2 hours of focused work.
4. **De-duplicate strings** — collapse the 104 duplicates into shared keys to prevent future drift.
5. **Document a tiny voice spec** — 1 page covering: «вы» (always), ё (always), ellipsis (`…`), error pattern (problem + next step), button label cap (≤24 chars). Once written, every future PR can be reviewed against it.
6. **Lint in CI** — the mechanical issues above (HTML balance, trailing whitespace, mixed ellipsis, button length, ё inconsistency on a fixed allow-list) are all detectable by a 50-line Python script gating PRs.

---
*Review generated 2026-05-01. File reviewed: `app/localization/locales/ru.json` @ HEAD (commit `57de4053`).*
