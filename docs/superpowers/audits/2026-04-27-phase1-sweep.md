# Phase 1 Static Sweep — 2026-04-27

Scope: cross-cutting bug-class grep across `app/`, `bedolaga-cabinet/src/`, `migrations/`.

## Findings

| # | Pattern | File | Line | Snippet | Severity | Decision | Action |
|---|---------|------|------|---------|----------|----------|--------|
| 1 | P1 | app/database/database.py | 474 | `text(f'SELECT COALESCE(MAX({q_col}), 0) FROM {q_schema}.{q_table}')` — identifiers from information_schema, quoted via `_quote_ident` | info | accept-with-rationale | accept |
| 2 | P1 | app/database/database.py | 492 | `text(f'SELECT last_value, is_called FROM {q_seq_schema}.{q_seq_name}')` — sequence name parsed from `pg_get_serial_sequence`, quoted via `_quote_ident` | info | accept-with-rationale | accept |
| 3 | P1 | app/services/backup_service.py | 455 | `text(f'SELECT COUNT(*) FROM {table_name}')` — table_name from SQLAlchemy `inspect().get_table_names()` (schema reflection, not user input) | info | accept-with-rationale | accept |
| 4 | P1 | app/services/backup_service.py | 1569 | `text(f'TRUNCATE {tables_str} RESTART IDENTITY CASCADE')` — tables_str joined from hardcoded `all_tables` literal list (line 1435) | info | accept-with-rationale | accept |
| 5 | P1 | app/services/backup_service.py | 1579 | `text(f'TRUNCATE {table_name} CASCADE')` — table_name iterated over the same hardcoded `all_tables` list | info | accept-with-rationale | accept |
| 6 | P2 | app/ | - | No hits — pattern clean (previous fix in `app/handlers/admin/achievements.py::CONDITION_TYPES` holds) | info | accept-with-rationale | accept |
| 7 | P3 | app/handlers/admin/backup.py | 54 | `except: date_str = '?'` — bare except wrapping `datetime.fromisoformat` for backup-list display fallback (catches BaseException incl. KeyboardInterrupt) | low | accept-with-rationale | accept |
| 8 | P3 | app/handlers/admin/backup.py | 171 | `except: page = 1` — bare except wrapping `int(callback.data.split('_')[-1])` for pagination fallback | low | accept-with-rationale | accept |
| 9 | P3 | app/handlers/admin/backup.py | 216 | `except: date_str = 'Ошибка формата даты'` — bare except wrapping `datetime.fromisoformat` for backup-info display fallback | low | accept-with-rationale | accept |
| 10 | P3 | app/handlers/admin/referrals.py | 143 | `except: pass` — bare except around Telegram `callback.message.edit_text` (blocked-user / stale-message swallow) | low | accept-with-rationale | accept |
| 11 | P3 | app/handlers/admin/referrals.py | 1459 | `except: <fallback message>` — bare except wrapping diagnostic file parse error fallback | low | accept-with-rationale | accept |
| 12 | P3 | app/services/remnawave_service.py | 1654 | `except: pass` — bare except around `await db.rollback()` in error-cleanup path (re-raise would mask original error) | low | accept-with-rationale | accept |
| 13 | P4 | app/handlers/stars_payments.py | 310 | `__import__('re').compile(r'^[A-Za-z0-9_\-]{10,100}$')` — constant string literal, no user input | info | accept-with-rationale | accept |
| 14 | P5 | app/ | - | No hits — pattern clean (Telegram bot tokens, AWS keys, Stripe live keys, BOT_TOKEN literals) | info | accept-with-rationale | accept |
| 15 | P6 | app/cabinet/routes/admin_*.py | - | AST scan of 41 admin route files: every `@router.<verb>` and `@admin_router.<verb>` carries `Depends(require_permission(...))` (or `admin_required` / `get_current_admin` equivalent) in the function signature; 0 unprotected admin routes | info | accept-with-rationale | accept |
| 16 | P7 | app/cabinet/routes/contests.py | 142 | `user.balance_kopeks += int(round(amount * 100))` — no `lock_user_for_pricing` / `with_for_update` in surrounding ~80 lines (contest reward credit) | high | real-bug | queue-phase2 |
| 17 | P7 | app/database/crud/achievement.py | 492 | `user.balance_kopeks += template.reward_value` — unlocked achievement payout credit | high | real-bug | queue-phase2 |
| 18 | P7 | app/database/models.py | 1366 | `self.balance_kopeks += kopeks` — `User.add_balance` helper; locking is caller-side by design (see below) | info | accept-with-rationale | accept |
| 19 | P7 | app/database/models.py | 1370 | `self.balance_kopeks -= kopeks` — `User.subtract_balance` helper; locking is caller-side by design | info | accept-with-rationale | accept |
| 20 | P7 | app/handlers/admin/referrals.py | 636 | `target_user.balance_kopeks += amount_kopeks` — admin manual referral payout, no SELECT FOR UPDATE | medium | real-bug | queue-phase2 |
| 21 | P7 | app/handlers/stars_payments.py | 660 | `user.balance_kopeks -= amount_kopeks` — Stars purchase debit unlocked | high | real-bug | queue-phase2 |
| 22 | P7 | app/handlers/stars_payments.py | 680 | `user.balance_kopeks += unused_kopeks` — Stars refund unlocked | high | real-bug | queue-phase2 |
| 23 | P7 | app/services/account_merge_service.py | 641 | `primary.balance_kopeks += transferred_kopeks` — account-merge balance transfer unlocked | high | real-bug | queue-phase2 |
| 24 | P7 | app/services/payment/aurapay.py | 419 | `user.balance_kopeks += payment.amount_kopeks` — webhook credit, unlocked | high | real-bug | queue-phase2 |
| 25 | P7 | app/services/payment/cloudpayments.py | 292 | `user.balance_kopeks += amount_kopeks` — webhook credit, unlocked | high | real-bug | queue-phase2 |
| 26 | P7 | app/services/payment/cryptobot.py | 312 | `user.balance_kopeks += amount_kopeks` — webhook credit, unlocked | high | real-bug | queue-phase2 |
| 27 | P7 | app/services/payment/freekassa.py | 319 | `user.balance_kopeks += payment.amount_kopeks` — webhook credit, unlocked | high | real-bug | queue-phase2 |
| 28 | P7 | app/services/payment/heleket.py | 364 | `user.balance_kopeks += amount_kopeks` — webhook credit, unlocked | high | real-bug | queue-phase2 |
| 29 | P7 | app/services/payment/kassa_ai.py | 315 | `user.balance_kopeks += payment.amount_kopeks` — webhook credit, unlocked | high | real-bug | queue-phase2 |
| 30 | P7 | app/services/payment/mulenpay.py | 296 | `user.balance_kopeks += payment.amount_kopeks` — webhook credit, unlocked | high | real-bug | queue-phase2 |
| 31 | P7 | app/services/payment/pal24.py | 430 | `user.balance_kopeks += payment.amount_kopeks` — webhook credit, unlocked | high | real-bug | queue-phase2 |
| 32 | P7 | app/services/payment/paypear.py | 408 | `user.balance_kopeks += payment.amount_kopeks` — webhook credit, unlocked | high | real-bug | queue-phase2 |
| 33 | P7 | app/services/payment/platega.py | 425 | `user.balance_kopeks += payment.amount_kopeks` — webhook credit, unlocked | high | real-bug | queue-phase2 |
| 34 | P7 | app/services/payment/rollypay.py | 414 | `user.balance_kopeks += payment.amount_kopeks` — webhook credit, unlocked | high | real-bug | queue-phase2 |
| 35 | P7 | app/services/payment/severpay.py | 410 | `user.balance_kopeks += payment.amount_kopeks` — webhook credit, unlocked | high | real-bug | queue-phase2 |
| 36 | P7 | app/services/payment/stars.py | 424 | `user.balance_kopeks += amount_kopeks` — webhook credit, unlocked | high | real-bug | queue-phase2 |
| 37 | P7 | app/services/payment/wata.py | 517 | `user.balance_kopeks += payment.amount_kopeks` — webhook credit, unlocked | high | real-bug | queue-phase2 |
| 38 | P7 | app/services/payment/yookassa.py | 769 | `user.balance_kopeks += payment.amount_kopeks` — webhook credit, unlocked | high | real-bug | queue-phase2 |
| 39 | P7 | app/services/promocode_service.py | 521 | `promocode.current_uses -= 1` — counter decrement (rollback on failed redemption) without `with_for_update` on the promocode row | medium | real-bug | queue-phase2 |
| 40 | P7 | app/services/tribute_service.py | 154 | `user.balance_kopeks += amount_kopeks` — Tribute donation credit, unlocked | high | real-bug | queue-phase2 |
| 41 | P7 | app/services/tribute_service.py | 273 | `user.balance_kopeks -= amount_kopeks` — Tribute subscription debit, unlocked | high | real-bug | queue-phase2 |
| 42 | P7 | app/services/tribute_service.py | 426 | `user.balance_kopeks += amount_kopeks` — Tribute refund credit, unlocked | high | real-bug | queue-phase2 |
| 43 | P7 | app/services/wheel_service.py | 321 | `user.balance_kopeks -= kopeks` — wheel-spin cost debit, unlocked | high | real-bug | queue-phase2 |
| 44 | P8 | bedolaga-cabinet/src/components/common/TelegramHtml.tsx | 92 | `<Tag dangerouslySetInnerHTML={{ __html: sanitized }} />` — sanitized via dedicated `telegramPurify.sanitize(...)` with strict ALLOWED_TAGS / ALLOWED_ATTR (Telegram HTML subset) | info | accept-with-rationale | accept |
| 45 | P8 | bedolaga-cabinet/src/components/connection/InstallationGuide.tsx | 231 | `dangerouslySetInnerHTML={{ __html: currentPlatformSvg }}` — value from `getSvgHtml(...)` which calls `DOMPurify.sanitize(raw, { USE_PROFILES: { svg: true, svgFilters: true } })` | info | accept-with-rationale | accept |
| 46 | P8 | bedolaga-cabinet/src/components/connection/InstallationGuide.tsx | 297 | `dangerouslySetInnerHTML={{ __html: appIconSvg }}` — same `getSvgHtml` DOMPurify svg-profile sanitisation | info | accept-with-rationale | accept |
| 47 | P8 | bedolaga-cabinet/src/components/connection/blocks/ThemeIcon.tsx | 30 | `dangerouslySetInnerHTML={{ __html: svgHtml }}` — `svgHtml` produced by `getSvgHtml` (DOMPurify svg-profile) and passed in as prop | info | accept-with-rationale | accept |
| 48 | P8 | bedolaga-cabinet/src/components/connection/blocks/BlockButtons.tsx | 100 | `dangerouslySetInnerHTML={{ __html: btnSvg }}` — `btnSvg` from `getSvgHtml` DOMPurify svg-profile sanitisation | info | accept-with-rationale | accept |
| 49 | P8 | bedolaga-cabinet/src/pages/AdminUpdates.tsx | 224 | `dangerouslySetInnerHTML={{ __html: bodyHtml }}` — `bodyHtml` from `renderMarkdown(...)` whose final step is `DOMPurify.sanitize(blocks.join(''), {...})` with explicit allowlists | info | accept-with-rationale | accept |
| 50 | P8 | bedolaga-cabinet/src/pages/NewsArticle.tsx | 425 | `dangerouslySetInnerHTML={{ __html: sanitizedContent }}` — local DOMPurify with strict ALLOWED_TAGS, iframe-host allowlist hook, defense-in-depth comments | info | accept-with-rationale | accept |
| 51 | P8 | bedolaga-cabinet/src/pages/Info.tsx | 256 | `dangerouslySetInnerHTML={{ __html: formatContent(faq.content) }}` — `formatContent` ends in `sanitizeHtml(...)` which calls DOMPurify with explicit allowlist | info | accept-with-rationale | accept |
| 52 | P8 | bedolaga-cabinet/src/pages/Info.tsx | 280 | `dangerouslySetInnerHTML={{ __html: formatContent(rules.content) }}` — same `formatContent` -> `sanitizeHtml` (DOMPurify) path | info | accept-with-rationale | accept |
| 53 | P8 | bedolaga-cabinet/src/pages/Info.tsx | 305 | `dangerouslySetInnerHTML={{ __html: formatContent(privacy.content) }}` — same `formatContent` -> `sanitizeHtml` (DOMPurify) path | info | accept-with-rationale | accept |
| 54 | P8 | bedolaga-cabinet/src/pages/Info.tsx | 330 | `dangerouslySetInnerHTML={{ __html: formatContent(offer.content) }}` — same `formatContent` -> `sanitizeHtml` (DOMPurify) path | info | accept-with-rationale | accept |
| 55 | P8 | bedolaga-cabinet/src/pages/HelpArticle.tsx | 233 | `dangerouslySetInnerHTML={{ __html: sanitizedContent }}` — `sanitizedContent` from `sanitizeHelpHtml(...)` (DOMPurify) with explicit allowlist + ALLOWED_URI_REGEXP | info | accept-with-rationale | accept |
| 56 | P8 | bedolaga-cabinet/src/pages/QuickPurchase.tsx | 493 | `dangerouslySetInnerHTML={{ __html: sanitized }}` — `SanitizedHtml` wrapper: DOMPurify with strict allowlist + post-sanitise hook to set `rel="noopener noreferrer"` + `target="_blank"` on links | info | accept-with-rationale | accept |
| 57 | P9 | bedolaga-cabinet/src/ | - | No hits — `eval(` and `new Function(` patterns clean across `src/**/*.{ts,tsx}` | info | accept-with-rationale | accept |
| 58 | P9 | bedolaga-cabinet/src/utils/token.ts | 66 | `localStorage.setItem(TOKEN_KEYS.REFRESH, refreshToken)` — long-lived refresh JWT in localStorage (XSS-readable). Access token uses sessionStorage; refresh stays in localStorage to survive tab close. Recommend httpOnly+Secure cookie or short-lived in-memory rotation | high | real-bug | queue-phase2 |
| 59 | P9 | bedolaga-cabinet/src/utils/token.ts | 100 | `localStorage.setItem(TOKEN_KEYS.REFRESH, refreshInSession)` — `migrateFromLocalStorage` re-persists refresh token from sessionStorage into localStorage; same XSS exposure as #58 | high | real-bug | queue-phase2 |
| 60 | P10 | bedolaga-cabinet/src/ | - | All 17 `<a target="_blank">` hits across `*.tsx` carry `rel="noopener noreferrer"` (verified by Grep -B/-A 2 sweep). Pattern clean for `<a>` elements | info | accept-with-rationale | accept |
| 61 | P10 | bedolaga-cabinet/src/platform/adapters/WebAdapter.ts | 199 | `window.open(_url, '_blank')` (no features) — `openInvoice` web fallback. Quick-fixed to `'noopener,noreferrer'` | medium | real-bug | quick-fix-applied |
| 62 | P10 | bedolaga-cabinet/src/platform/adapters/TelegramAdapter.ts | 284 | `window.open(url, '_blank')` (no features) — `openInvoice` SDK-fail fallback. Quick-fixed to `'noopener,noreferrer'` | medium | real-bug | quick-fix-applied |
| 63 | P10 | bedolaga-cabinet/src/platform/adapters/TelegramAdapter.ts | 293 | `window.open(url, '_blank')` (no features) — `openLink` SDK-fail fallback. Quick-fixed to `'noopener,noreferrer'` | medium | real-bug | quick-fix-applied |
| 64 | P10 | bedolaga-cabinet/src/platform/adapters/TelegramAdapter.ts | 301 | `window.open(url, '_blank')` (no features) — `openTelegramLink` SDK-fail fallback. Quick-fixed to `'noopener,noreferrer'` | medium | real-bug | quick-fix-applied |
| 65 | P10 | bedolaga-cabinet/src/components/dashboard/TrialOfferCard.tsx | 400 | `window.open(telegramProxyUrl, '_blank')` (no features) — proxy-link button onClick. Quick-fixed to `'noopener,noreferrer'` | medium | real-bug | quick-fix-applied |
| 66 | P10 | bedolaga-cabinet/src/pages/Info.tsx | 537 | `window.open(proxyData.url!, '_blank')` (no features) — telegram-proxy Card onClick. Quick-fixed to `'noopener,noreferrer'` | medium | real-bug | quick-fix-applied |
| 67 | P10 | bedolaga-cabinet/src/pages/Wheel.tsx | 372 | `window.open('about:blank', '_blank')` — pre-opens window during user-gesture for popup-blocker workaround; `preOpenedWindowRef.current.location.href = data.invoice_url` later sets URL. Adding `noopener` would set the return to `null` and break the ref. Same-origin `about:blank` is low-risk | low | accept-with-rationale | accept |

(Severity: critical / high / medium / low / info.
 Decision: real-bug / false-positive / accept-with-rationale.
 Action: quick-fix-applied / queue-phase2 / accept.)

## Pattern catalogue

- P1: Raw SQL injection vectors
- P2: Surrogate-pair string escapes
- P3: Swallowed exceptions
- P4: Code execution sinks
- P5: Hardcoded secrets
- P6: Missing auth on admin endpoints
- P7: Money-path race conditions
- P8: dangerouslySetInnerHTML without sanitiser
- P9: eval / new Function / sensitive localStorage
- P10: target="_blank" without rel="noopener noreferrer"
- P11: SQL migration anti-patterns

## Summary

(filled at end of Phase 1)
- Total hits: TBD-fill
- Real bugs (quick-fixed): TBD-fill
- Real bugs (queued for Phase 2): TBD-fill
- False positives: TBD-fill
- Accepted with rationale: TBD-fill
