# Recurring Payments: Provider Abstraction

**Date**: 2026-05-19
**Target branch**: `feature/recurring-providers-abstraction`
**Base**: `upstream/main` (v3.56.0)
**Goal**: enable EtoPlatezhi (and future providers) to participate in saved-card recurring payments alongside the existing YooKassa-only implementation.

## Problem

Today the recurring/saved-card stack assumes a single provider:

- `SavedPaymentMethod.yookassa_payment_method_id` is the unique key
- `recurrent_payment_service.process_recurrent_payments()` calls `yookassa_service.create_autopayment(...)` directly
- Cabinet/TG handlers check `settings.YOOKASSA_RECURRENT_ENABLED` to gate the UI
- Webhook parsers store cards only for YooKassa

EtoPlatezhi supports its own recurring scheme (Card-on-File, recurring_id + account_token) but is wired only for one-shot payments. Tinkoff Acquiring, CloudPayments and others would face the same blockers.

## Design Principles

1. Provider-agnostic data model — one `saved_payment_methods` row regardless of which payment system holds the actual card-on-file token.
2. Thin, explicit provider abstraction — a Protocol with two operations: `register card` (parse from webhook) and `charge saved card`. Webhook routing stays per-provider (each gateway has its own signature scheme and URL).
3. Backward compatibility — existing YooKassa data keeps working without manual intervention, no breaking API/UI changes for end users.
4. Opt-in providers — each provider is gated by its own `*_RECURRENT_ENABLED` flag; cabinet/TG UI shows cards as long as **any** provider is enabled.

## Data Model

### Migration `0XXX_recurring_provider_columns.py`

Add to `saved_payment_methods`:

| Column | Type | Notes |
|---|---|---|
| `provider` | `String(32)` NOT NULL DEFAULT `'yookassa'` | `'yookassa'`, `'etoplatezhi'`, ... |
| `provider_token` | `String(255)` | unified token; YooKassa `payment_method.id` or EtoPlatezhi `recurring.id` |
| `valid_thru` | `TIMESTAMPTZ` | optional provider-side expiry (EtoPlatezhi returns this) |

Data migration: `UPDATE saved_payment_methods SET provider_token = yookassa_payment_method_id WHERE provider_token IS NULL AND yookassa_payment_method_id IS NOT NULL;`

`yookassa_payment_method_id` stays for backward compat (one minor release), then deprecated.

New unique index: `(provider, provider_token)` partial WHERE `provider_token IS NOT NULL`.

### `SavedPaymentMethod` model changes

Add `provider`, `provider_token`, `valid_thru` columns. Keep `yookassa_payment_method_id` until a follow-up cleanup release.

## Provider Abstraction

### `app/services/payment/recurring/base.py`

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

@dataclass
class CardRegistration:
    """Returned when a webhook reports a newly saved card."""
    provider_token: str
    method_type: str           # 'bank_card' | 'sbp' | ...
    card_first6: str | None
    card_last4: str | None
    card_type: str | None      # 'visa' | 'mastercard' | 'mir' | ...
    expiry_month: str | None
    expiry_year: str | None
    title: str | None
    valid_thru: datetime | None

@dataclass
class ChargeResult:
    success: bool
    provider_payment_id: str | None
    error_code: str | None
    error_message: str | None
    raw: dict | None           # for logging/audit

class RecurringProvider(ABC):
    name: str                  # 'yookassa', 'etoplatezhi'

    @abstractmethod
    def is_enabled(self) -> bool: ...

    @abstractmethod
    async def charge(
        self,
        provider_token: str,
        amount_kopeks: int,
        description: str,
        metadata: dict,
        idempotency_key: str,
    ) -> ChargeResult: ...

    # Optional: revoke = best-effort, providers may not support it
    async def revoke(self, provider_token: str) -> bool:
        return True
```

### Implementations

- `app/services/payment/recurring/yookassa_provider.py` — wraps existing `yookassa_service.create_autopayment(...)` and `_save_payment_method_if_available(...)` logic.
- `app/services/payment/recurring/etoplatezhi_provider.py` — new, calls EtoPlatezhi Gate `sale` with `stored_card_type: 4` + `recurring.id`. Signature/HTTP via existing `etoplatezhi_service`.

### Registry

```python
# app/services/payment/recurring/__init__.py
_PROVIDERS: dict[str, RecurringProvider] = {
    'yookassa': YooKassaRecurringProvider(),
    'etoplatezhi': EtoPlatezhiRecurringProvider(),
}

def get_provider(name: str) -> RecurringProvider:
    return _PROVIDERS[name]

def enabled_providers() -> list[RecurringProvider]:
    return [p for p in _PROVIDERS.values() if p.is_enabled()]
```

## Touchpoints

### CRUD (`app/database/crud/saved_payment_method.py`)

- `create_saved_payment_method(db, user_id, provider, provider_token, **card_fields)` — replaces YooKassa-specific signature. During refactor, add an overload that takes `yookassa_payment_method_id=` and auto-fills `provider='yookassa'`, `provider_token=value` for callers we haven't migrated yet.
- New: `get_payment_method_by_provider_token(db, provider, provider_token)`.
- Keep: `get_active_payment_methods_by_user`, `deactivate_payment_method`, `get_user_ids_with_active_payment_methods` (no API change).

### Recurring charge orchestrator (`app/services/recurrent_payment_service.py`)

Replace the hard `yookassa_service.create_autopayment(...)` call (line ~333) with:

```python
provider = get_provider(saved_method.provider)
result = await provider.charge(
    provider_token=saved_method.provider_token,
    amount_kopeks=int(topup_amount_rubles * 100),
    description=description,
    metadata=metadata,
    idempotency_key=idem_key,
)
if not result.success:
    continue
# rest stays the same: persist payment locally, notify user
```

### Webhook handlers

No router changes. Internal handlers extract `CardRegistration` from the parsed event and call the new CRUD overload. YooKassa keeps using `payment_method_data.saved=true`; EtoPlatezhi parses `recurring.id` + `account.token` from the success callback.

### Cabinet + TG

- `GET/DELETE /balance/saved-cards` and `app/handlers/subscription/autopay.py` — replace `if not settings.YOOKASSA_RECURRENT_ENABLED` with `if not enabled_providers()`. List response includes `provider` field so the client can show a small provider badge.

### Config (`app/config.py`)

Add:
- `ETOPLATEZHI_RECURRENT_ENABLED: bool = False`
- `ETOPLATEZHI_RECURRENT_REQUIRED: bool = False` (analog of YooKassa flag — force `stored_card_type=3` on initial payment)

## Backward Compatibility

- Existing rows: data migration copies `yookassa_payment_method_id → provider_token` and sets `provider='yookassa'`.
- Existing callers of `create_saved_payment_method(yookassa_payment_method_id=...)` continue to work — the keyword is kept as an alias for one release.
- Existing cabinet/TG flow: same JSON shape for the card list, just gains an optional `provider` field.
- Existing autopay path: same orchestrator function name and signature; only its body changes.

## Rollout / Testing

1. Migration on dev DB → verify backfill produces expected `provider_token` for every active YooKassa row.
2. Smoke test YooKassa recurring on staging: register card → trigger autopay → confirm charge.
3. Smoke test EtoPlatezhi: one-time payment with `stored_card_type=3` → verify card row created with `provider='etoplatezhi'` and `provider_token=<recurring.id>` → trigger autopay → confirm charge.
4. Unit tests: provider registry, CRUD migration overload, monitoring path with mocked providers.
5. Manual QA: cabinet UI shows both providers, delete works for both.

## Out of Scope (future PRs)

- Tinkoff Acquiring / CloudPayments providers — would just add another file under `app/services/payment/recurring/`.
- Multi-card "default" picker — current logic charges the first active card; can be refined later.
- Provider-side card validity refresh (some gateways notify when card expired).

## Open Questions

1. EtoPlatezhi `recurring_id` returned by the platform — sometimes integer, sometimes string. Store as string in `provider_token` to be safe; document in code.
2. `valid_thru` is provider-specific; YooKassa doesn't expose it. Leave NULL for YooKassa rows.
3. PR strategy — single PR or split into "migration + abstraction" + "EtoPlatezhi provider"? Recommend split: smaller diffs review more easily.

## File Footprint (estimated)

| File | Change |
|---|---|
| `migrations/alembic/versions/0XXX_recurring_provider_columns.py` | new |
| `app/database/models.py` | + 3 columns on `SavedPaymentMethod` |
| `app/database/crud/saved_payment_method.py` | rework CRUD; keep BC alias |
| `app/services/payment/recurring/__init__.py` | new (registry) |
| `app/services/payment/recurring/base.py` | new (abstraction) |
| `app/services/payment/recurring/yookassa_provider.py` | new (wraps existing code) |
| `app/services/payment/recurring/etoplatezhi_provider.py` | new |
| `app/services/recurrent_payment_service.py` | swap to provider dispatch |
| `app/services/payment/yookassa.py` | adapt webhook `_save_payment_method_if_available` |
| `app/services/payment/etoplatezhi.py` | parse + persist recurring registration |
| `app/services/etoplatezhi_service.py` | add `build_recurring_charge_payload()` |
| `app/cabinet/routes/balance.py` | gate by `enabled_providers()` |
| `app/cabinet/schemas/balance.py` | add `provider` field |
| `app/handlers/subscription/autopay.py` | gate + minor copy |
| `app/config.py` | + EtoPlatezhi recurring flags |

~14 files. Two atomic commits per phase (migration / abstraction / EtoPlatezhi / UI).
