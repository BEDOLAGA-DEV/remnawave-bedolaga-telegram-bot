"""Pure byte accounting for finite traffic entitlements.

No database, panel writes, scheduler, or migration is performed here. Callers
must serialize sampling, purchases and expiry on the subscription, and settle
usage BEFORE issuing a new allowance. A reset of the panel counter is not an
entitlement grant. The separate base refresh operation supplies that grant.
"""

from dataclasses import dataclass, replace
from datetime import datetime


class AccountingConflict(ValueError):
    """A counter or operation cannot be safely applied to this account."""


@dataclass(frozen=True)
class Allowance:
    key: str
    remaining_bytes: int
    expires_at: datetime
    granted_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.granted_bytes is None:
            object.__setattr__(self, 'granted_bytes', self.remaining_bytes)
        if not self.key or self.expires_at.tzinfo is None:
            raise AccountingConflict('Allowance requires a key and an aware expiry')
        if not isinstance(self.remaining_bytes, int) or not isinstance(self.granted_bytes, int):
            raise AccountingConflict('Traffic amounts must be integer bytes')
        if not 0 <= self.remaining_bytes <= self.granted_bytes:
            raise AccountingConflict('Invalid allowance amount')


@dataclass(frozen=True)
class TrafficAccount:
    panel_identity: str
    lifetime_bytes: int
    used_bytes: int
    allowances: tuple[Allowance, ...]

    def __post_init__(self) -> None:
        _validate_counters(self.lifetime_bytes, self.used_bytes)
        if not self.panel_identity or len({item.key for item in self.allowances}) != len(self.allowances):
            raise AccountingConflict('Invalid identity or duplicate allowance keys')

    def remaining_bytes(self, now: datetime) -> int:
        _validate_time(now)
        return sum(item.remaining_bytes for item in self.allowances if item.expires_at > now)

    def panel_limit_bytes(self, now: datetime) -> int:
        # Remnawave interprets 0 as unlimited. When remaining=0 the caller must
        # also prohibit access with a supported panel status (including the used=0 case); 1 is only a sentinel.
        return max(1, self.used_bytes + self.remaining_bytes(now))

    def add(self, allowance: Allowance) -> 'TrafficAccount':
        for existing in self.allowances:
            if existing.key == allowance.key:
                if existing.granted_bytes != allowance.granted_bytes or existing.expires_at != allowance.expires_at:
                    raise AccountingConflict('Allowance key reused with different parameters')
                return self
        return replace(self, allowances=(*self.allowances, allowance))


def _validate_time(now: datetime) -> None:
    if now.tzinfo is None:
        raise AccountingConflict('Accounting requires timezone-aware timestamps')


def _validate_counters(lifetime: int, used: int) -> None:
    if not isinstance(lifetime, int) or not isinstance(used, int) or not 0 <= used <= lifetime:
        raise AccountingConflict('Invalid traffic counters')


def observe_usage(
    account: TrafficAccount,
    panel_identity: str,
    lifetime_bytes: int,
    used_bytes: int,
    now: datetime,
) -> TrafficAccount:
    """Settle the next sample, then expire remainders; never create a debt.

    Pending usage is allocated before expiry because it may have occurred
    before that boundary. The panel API supplies aggregate counters, not a
    per-byte timestamp. Callers must sample and project at expiry boundaries;
    this function cannot reconstruct timing within an unobserved interval.
    Identity changes/counter regressions require explicit reconciliation.
    """
    _validate_time(now)
    _validate_counters(lifetime_bytes, used_bytes)
    if panel_identity != account.panel_identity or lifetime_bytes < account.lifetime_bytes:
        raise AccountingConflict('Panel identity or cumulative counter changed')
    pending = lifetime_bytes - account.lifetime_bytes
    if used_bytes > account.used_bytes + pending:
        raise AccountingConflict('Resettable counter increased beyond cumulative usage')
    balances = {}
    for allowance in sorted(account.allowances, key=lambda item: (item.expires_at, item.key)):
        consumed = min(allowance.remaining_bytes, pending)
        pending -= consumed
        balances[allowance.key] = replace(
            allowance,
            remaining_bytes=allowance.remaining_bytes - consumed if allowance.expires_at > now else 0,
        )
    return replace(
        account,
        lifetime_bytes=lifetime_bytes,
        used_bytes=used_bytes,
        allowances=tuple(balances[item.key] for item in account.allowances),
    )


def refresh_base(account: TrafficAccount, allowance: Allowance) -> TrafficAccount:
    """Replace the base allowance once per period/operation, preserving packages."""
    if not allowance.key.startswith('base:'):
        raise AccountingConflict('Base allowance needs a base operation key')
    updated = account.add(allowance)
    if updated is account:
        return account
    return replace(
        updated,
        allowances=tuple(
            replace(item, remaining_bytes=0) if item.key.startswith('base:') and item.key != allowance.key else item
            for item in updated.allowances
        ),
    )


def migrate_preserving_available(
    *,
    panel_identity: str,
    lifetime_bytes: int,
    used_bytes: int,
    panel_limit_bytes: int,
    allowances: tuple[Allowance, ...],
    now: datetime,
    carry_expires_at: datetime,
) -> TrafficAccount:
    """Bootstrap a finite account without increasing or reducing available bytes.

    Old per-package consumption is unknowable. FEFO allocation is a migration
    convention, NOT recovered history. Runtime/UI must mark it as such. Any
    availability exceeding identifiable active grants is kept as an explicit
    legacy carry, never silently discarded. Its expiry must be chosen by the
    caller from the existing entitlement/calendar, not guessed in this function.
    """
    _validate_counters(lifetime_bytes, used_bytes)
    _validate_time(now)
    _validate_time(carry_expires_at)
    if type(panel_limit_bytes) is not int or panel_limit_bytes <= 0:
        raise AccountingConflict('Only finite positive panel limits can be migrated')
    if carry_expires_at <= now:
        raise AccountingConflict('Legacy carry must have a future expiry')
    available = max(0, panel_limit_bytes - used_bytes)
    if any(item.key == 'legacy:carry' for item in allowances):
        raise AccountingConflict('Legacy carry is reserved for the migration result')
    active = tuple(item for item in allowances if item.expires_at > now)
    nominal = sum(item.remaining_bytes for item in active)
    consumed = max(0, nominal - available)
    allocated = {}
    for item in sorted(active, key=lambda item: (item.expires_at, item.key)):
        debit = min(item.remaining_bytes, consumed)
        consumed -= debit
        allocated[item.key] = replace(item, remaining_bytes=item.remaining_bytes - debit)
    migrated = TrafficAccount(panel_identity, lifetime_bytes, used_bytes, tuple(allocated[item.key] for item in active))
    carry = available - migrated.remaining_bytes(now)
    if carry:
        migrated = migrated.add(Allowance('legacy:carry', carry, carry_expires_at))
    return migrated
