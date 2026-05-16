"""
RBAC bootstrap service.

Auto-assigns the Superadmin role to users listed in ADMIN_IDS / ADMIN_EMAILS
config on bot startup. Runs once during the startup sequence.
"""

import unicodedata
from dataclasses import dataclass
from typing import Final

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.rbac import SUPERADMIN_LEVEL, UserRoleCRUD
from app.database.models import AdminAuditLog, AdminRole, User, UserRole


def normalize_admin_email(value: str) -> str:
    """Канонизация email для сравнения с ADMIN_EMAILS: NFKC + lower + strip.

    Защищает от fullwidth-character bypass (`ＡＤＭＩＮ＠example.com` → `admin@example.com`).
    Cyrillic-vs-Latin homographs остаются разными — NFKC их не объединяет, защита от
    confusables-атак вне scope (требует confusables-detect библиотеки).

    Используется и в bootstrap, и в login-time, и в auto_login guard — единая точка
    нормализации, чтобы drift между путями был невозможен.
    """
    return unicodedata.normalize('NFKC', value).strip().lower()


# Backward-compat alias на случай если что-то импортировало private имя.
_normalize_email = normalize_admin_email


def _mask_email(value: str | None) -> str:
    """Маскинг email для логов: `admin@example.com` → `a***@e***.com`."""
    if not value or '@' not in value:
        return '***'
    local, _, domain = value.partition('@')
    domain_name, _, tld = domain.rpartition('.')
    local_mask = (local[:1] + '***') if local else '***'
    domain_mask = (domain_name[:1] + '***') if domain_name else '***'
    return f'{local_mask}@{domain_mask}.{tld}' if tld else f'{local_mask}@{domain_mask}'


# Источники верификации email, которым доверяем для admin escalation.
# - cabinet: юзер ввёл OTP-код, отправленный кабинетом — реальный proof of ownership.
# - oauth_google: OIDC userinfo over TLS, Google enforces email verification.
# - oauth_discord: Discord API `verified` flag — провайдер сам проверяет.
# - admin_override: установлено вручную через admin UI / migrations.
# VK / Yandex отсутствуют — их `email_verified=True` обусловлен лишь наличием
# email в OAuth-ответе, но провайдер не выдаёт cryptographic proof of ownership.
# Email используется для UX (recovery, linking, panel sync), но не доверяется
# для match с ADMIN_EMAILS.
TRUSTED_EMAIL_VERIFICATION_SOURCES: 'frozenset[str]' = frozenset(
    {'cabinet', 'oauth_google', 'oauth_discord', 'admin_override'}
)


@dataclass(frozen=True)
class AdminEnvCheck:
    """Результат проверки 'этот юзер админ по ENV-конфигу?'

    Используется как login-time, так и bootstrap-time, и в auto_login guard.
    Single source of truth — раньше эта же логика была размазана inline по 4 местам
    и уже один раз дрейфила (NFKC vs lower).
    """

    is_telegram_admin: bool
    is_email_admin: bool

    @property
    def is_admin(self) -> bool:
        return self.is_telegram_admin or self.is_email_admin

    @property
    def matched_via(self) -> str | None:
        if self.is_telegram_admin:
            return 'telegram_id'
        if self.is_email_admin:
            return 'email'
        return None


def is_user_admin_by_env(user: User) -> AdminEnvCheck:
    """Проверяет, является ли юзер админом по ENV-конфигу (ADMIN_IDS/ADMIN_EMAILS).

    Включает:
    - фильтр положительных telegram_id (id=0 в ADMIN_IDS не повышает sentinel-юзера),
    - NFKC-нормализацию email с обеих сторон,
    - проверку email_verified (для email-пути),
    - проверку email_verification_source против trusted-провайдеров (если поле есть).

    Не делает SQL-запросов — чистая функция от User-row + settings.
    """
    admin_ids: set[int] = {tg_id for tg_id in (settings.get_admin_ids() or []) if tg_id > 0}
    admin_emails: set[str] = {normalize_admin_email(email) for email in (settings.get_admin_emails() or []) if email}

    try:
        telegram_id_int = int(user.telegram_id) if user.telegram_id is not None else None
    except (TypeError, ValueError):
        telegram_id_int = None
    is_telegram_admin = telegram_id_int is not None and telegram_id_int > 0 and telegram_id_int in admin_ids

    user_email = getattr(user, 'email', None)
    email_verified = bool(getattr(user, 'email_verified', False))
    # Trust guard: для admin escalation нужен НЕ просто verified email, но и
    # верификация через trusted источник. VK/Yandex выставляют email_verified=True
    # для UX (recovery, linking), но их source='oauth_vk'/'oauth_yandex' НЕ в
    # TRUSTED_EMAIL_VERIFICATION_SOURCES — значит admin escalation для них закрыта.
    # NULL (legacy строки до миграции 0079, не успевшие пройти backfill) трактуется
    # как trusted-'cabinet' equivalent — backward-compat.
    verification_source = getattr(user, 'email_verification_source', None)
    verification_ok = email_verified and (
        verification_source is None or verification_source in TRUSTED_EMAIL_VERIFICATION_SOURCES
    )

    is_email_admin = (
        user_email is not None
        and bool(user_email)
        and verification_ok
        and normalize_admin_email(user_email) in admin_emails
    )

    return AdminEnvCheck(is_telegram_admin=is_telegram_admin, is_email_admin=is_email_admin)


logger = structlog.get_logger(__name__)

SUPERADMIN_ROLE_NAME: Final[str] = 'Superadmin'

# Preset roles seeded on first run
_PRESET_ROLES: list[dict] = [
    {
        'name': 'Superadmin',
        'description': 'Full system access',
        'level': 999,
        'permissions': ['*:*'],
        'color': '#EF4444',
        'icon': 'shield',
        'is_system': True,
    },
    {
        'name': 'Admin',
        'description': 'Administrative access',
        'level': 100,
        'permissions': [
            'users:*',
            'tickets:*',
            'stats:*',
            'sales_stats:*',
            'broadcasts:*',
            'tariffs:*',
            'promocodes:*',
            'promo_groups:*',
            'promo_offers:*',
            'campaigns:*',
            'partners:*',
            'withdrawals:*',
            'payments:*',
            'payment_methods:*',
            'servers:*',
            'remnawave:*',
            'traffic:*',
            'settings:*',
            'roles:read',
            'roles:create',
            'roles:edit',
            'roles:assign',
            'audit_log:*',
            'channels:*',
            'ban_system:*',
            'wheel:*',
            'apps:*',
            'email_templates:*',
            'pinned_messages:*',
            'updates:*',
            'landings:read',
            'landings:create',
            'landings:edit',
            'landings:delete',
        ],
        'color': '#F59E0B',
        'icon': 'crown',
        'is_system': True,
    },
    {
        'name': 'Moderator',
        'description': 'User and ticket management',
        'level': 50,
        'permissions': ['users:read', 'users:edit', 'users:block', 'tickets:*', 'ban_system:*'],
        'color': '#3B82F6',
        'icon': 'user-shield',
        'is_system': True,
    },
    {
        'name': 'Marketer',
        'description': 'Marketing tools access',
        'level': 30,
        'permissions': [
            'campaigns:*',
            'broadcasts:*',
            'promocodes:*',
            'promo_offers:*',
            'promo_groups:*',
            'stats:read',
            'sales_stats:read',
            'pinned_messages:*',
            'wheel:*',
        ],
        'color': '#8B5CF6',
        'icon': 'megaphone',
        'is_system': True,
    },
    {
        'name': 'Support',
        'description': 'Ticket support access',
        'level': 20,
        'permissions': ['tickets:read', 'tickets:reply', 'users:read'],
        'color': '#10B981',
        'icon': 'headset',
        'is_system': True,
    },
]


async def _ensure_preset_roles(db: AsyncSession) -> AdminRole | None:
    """Seed preset roles if they don't exist. Returns the Superadmin role.

    Системные роли идентифицируются по (is_system=True, level) — это стабильно
    даже если админ переименовал роль через UI.
    Fallback на поиск по имени для обратной совместимости.
    """
    superadmin_role: AdminRole | None = None

    for preset in _PRESET_ROLES:
        # Сначала ищем по стабильному ключу (is_system + level)
        result = await db.execute(
            select(AdminRole).where(AdminRole.is_system.is_(True), AdminRole.level == preset['level'])
        )
        existing = result.scalars().first()

        # Fallback: поиск по имени (для ролей, созданных до этого фикса)
        if existing is None:
            result = await db.execute(select(AdminRole).where(AdminRole.name == preset['name']))
            existing = result.scalars().first()

        if existing is not None:
            if existing.level == SUPERADMIN_LEVEL:
                superadmin_role = existing
            # Добавить НОВЫЕ permissions из кода, не трогая существующие (админ мог кастомизировать)
            if existing.is_system:
                current = set(existing.permissions or [])
                from_code = set(preset['permissions'])
                new_perms = from_code - current
                if new_perms:
                    existing.permissions = list(current | new_perms)
                    await db.flush()
                    logger.info(
                        'Added new permissions to system role',
                        role_name=existing.name,
                        role_id=existing.id,
                        added=sorted(new_perms),
                    )
            continue

        role = AdminRole(
            name=preset['name'],
            description=preset['description'],
            level=preset['level'],
            permissions=preset['permissions'],
            color=preset['color'],
            icon=preset['icon'],
            is_system=preset['is_system'],
            is_active=True,
        )
        db.add(role)
        await db.flush()
        logger.info('Seeded preset role', role_name=preset['name'], role_id=role.id)

        if preset['name'] == SUPERADMIN_ROLE_NAME:
            superadmin_role = role

    return superadmin_role


async def bootstrap_superadmins(db: AsyncSession) -> None:
    """Ensure every user from ADMIN_IDS / ADMIN_EMAILS has the Superadmin role.

    Also seeds preset roles on first run.
    Idempotent: skips users who already hold an active Superadmin assignment.
    Commits only when at least one change was made.
    """
    try:
        admin_ids = settings.get_admin_ids()
        admin_emails = settings.get_admin_emails()

        # ── 1. Ensure preset roles exist (seeds on first run) ──────────
        superadmin_role = await _ensure_preset_roles(db)

        if superadmin_role is None:
            logger.error('Failed to resolve Superadmin role after seeding')
            return

        if not admin_ids and not admin_emails:
            logger.debug('No admin IDs or emails configured, skipping superadmin assignment')
            await db.commit()
            # Safety check even when no IDs configured — someone may have cleared them
            await _warn_if_no_superadmins(db, admin_ids, admin_emails)
            return

        role_id: int = superadmin_role.id
        assigned_count = 0

        # ── 2. Process admin telegram IDs ──────────────────────────────
        for telegram_id in admin_ids:
            assigned = await _ensure_role_by_telegram_id(db, telegram_id=telegram_id, role_id=role_id)
            if assigned:
                assigned_count += 1

        # ── 3. Process admin emails ────────────────────────────────────
        for email in admin_emails:
            assigned = await _ensure_role_by_email(db, email=email, role_id=role_id)
            if assigned:
                assigned_count += 1

        # ── 4. Revoke superadmin from users NOT in env ───────────────
        revoked_count = await _revoke_stale_superadmins(
            db,
            role_id=role_id,
            admin_ids=admin_ids,
            admin_emails=admin_emails,
        )

        # ── 5. Commit all changes ──────────────────────────────────────
        await db.commit()

        if assigned_count > 0 or revoked_count > 0:
            logger.info(
                'Superadmin bootstrap completed',
                assigned_count=assigned_count,
                revoked_count=revoked_count,
                role_id=role_id,
            )
        else:
            logger.debug('Superadmin bootstrap: no changes needed')

        # ── 6. Safety: warn if no active superadmins exist ────────────
        await _warn_if_no_superadmins(db, admin_ids, admin_emails)

    except Exception:
        await db.rollback()
        logger.exception('Failed to bootstrap superadmins, continuing startup')


async def _revoke_stale_superadmins(
    db: AsyncSession,
    *,
    role_id: int,
    admin_ids: list[int],
    admin_emails: list[str],
) -> int:
    """Revoke superadmin from users who are no longer in env config.

    Env config (ADMIN_IDS / ADMIN_EMAILS) is the single source of truth.
    If a user was removed from env, their superadmin DB role is deactivated
    on the next bot restart.

    Returns the number of revoked assignments.
    """
    result = await db.execute(
        select(UserRole)
        .options(selectinload(UserRole.user))
        .where(
            UserRole.role_id == role_id,
            UserRole.is_active.is_(True),
        )
    )
    active_assignments = result.scalars().all()

    admin_ids_set = set(admin_ids)
    # NFKC-нормализуем env-emails и сравниваем нормализованную форму user.email с этим
    # set — иначе fullwidth-character мог бы попасть в одну сторону и не совпасть с
    # другой. Согласовано с ensure_superadmin_role_on_login.
    admin_emails_set = {normalize_admin_email(e) for e in admin_emails}

    revoked = 0
    for assignment in active_assignments:
        user = assignment.user
        if user is None:
            continue

        # Check if user is still in env config.
        # email_verified is required — symmetric with _ensure_role_by_email.
        in_env_by_id = user.telegram_id is not None and user.telegram_id in admin_ids_set
        in_env_by_email = (
            user.email is not None and user.email_verified and normalize_admin_email(user.email) in admin_emails_set
        )

        if not in_env_by_id and not in_env_by_email:
            # Защита от race / повторного запуска bootstrap: если запись уже
            # неактивна И revocation_source='ui' — НЕ перезаписываем причину.
            # Иначе senior-админ revoke через UI был бы переименован в env-revoke
            # на ближайшем рестарте, теряя forensic trail и нарушая семантику
            # 'ui' branch в _assign_if_missing.
            if not assignment.is_active and assignment.revocation_source == 'ui':
                continue

            assignment.is_active = False
            assignment.revocation_source = 'env'
            await db.flush()
            revoked += 1
            logger.warning(
                'Revoked Superadmin role: user removed from env config',
                user_id=user.id,
                telegram_id=user.telegram_id,
                email=_mask_email(user.email),
                user_role_id=assignment.id,
                revocation_source='env',
            )

    return revoked


async def _warn_if_no_superadmins(
    db: AsyncSession,
    admin_ids: list[int],
    admin_emails: list[str],
) -> None:
    """Log critical/warning if no active superadmin RBAC roles exist in DB."""
    active = await UserRoleCRUD.get_superadmin_count(db)
    if active > 0:
        return
    if not admin_ids and not admin_emails:
        logger.critical(
            'No active superadmins exist and no ADMIN_IDS/ADMIN_EMAILS configured. '
            'Cabinet admin access is not possible until this is resolved.',
        )
    else:
        logger.warning(
            'No active superadmin RBAC roles in DB. Legacy config admins (ADMIN_IDS/ADMIN_EMAILS) still have access.',
        )


async def _ensure_role_by_telegram_id(
    db: AsyncSession,
    *,
    telegram_id: int,
    role_id: int,
) -> bool:
    """Assign Superadmin role to user found by telegram_id. Returns True if assigned."""
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()

    if user is None:
        logger.debug(
            'Admin user not yet registered, skipping',
            telegram_id=telegram_id,
        )
        return False

    return await _assign_if_missing(db, user_id=user.id, role_id=role_id, identifier=str(telegram_id))


async def ensure_superadmin_role_on_login(db: AsyncSession, user: User) -> bool:
    """Idempotent Superadmin assign for ADMIN_IDS / ADMIN_EMAILS users at login time.

    Возвращает True если роль была назначена прямо сейчас (новая запись), False во всех
    остальных случаях — включая случай, когда у юзера уже есть UserRole-запись
    (active или deactivated).

    Назначение: покрыть кейс «удалён через кабинет → пересоздан через /start с новым
    user.id → бот не рестартовал → RBAC bootstrap не отработал». В этом сценарии у
    нового user.id вообще НЕТ записи в user_roles, и эта функция создаёт её.

    ВАЖНО — в отличие от bootstrap-времени `_assign_if_missing`, здесь НЕ реактивируем
    deactivated роль. Если senior admin отозвал Superadmin через UI (поставил
    is_active=False), при следующем login юзера мы это уважаем. Реактивация env-grants
    происходит только при старте бота (через `_revoke_stale_superadmins` + текущий
    bootstrap), чтобы не превратить login в способ обхода manual revoke.
    """
    env_check = is_user_admin_by_env(user)
    if not env_check.is_admin:
        return False
    is_telegram_admin = env_check.is_telegram_admin
    user_email = getattr(user, 'email', None)

    # Lookup Superadmin role: предпочитаем поиск по (is_system=True, level=SUPERADMIN_LEVEL)
    # как в _ensure_preset_roles — это работает даже если кто-то переименовал «Superadmin»
    # через UI. Fallback на name='Superadmin' для совместимости со старыми seed-данными.
    role_result = await db.execute(
        select(AdminRole).where(
            AdminRole.is_system.is_(True),
            AdminRole.level == SUPERADMIN_LEVEL,
        )
    )
    role = role_result.scalar_one_or_none()
    if role is None:
        role_result = await db.execute(select(AdminRole).where(AdminRole.name == SUPERADMIN_ROLE_NAME))
        role = role_result.scalar_one_or_none()
    if role is None:
        logger.warning(
            'Superadmin role not found at login bootstrap — RBAC tables not seeded?',
            user_id=user.id,
        )
        return False

    # Проверяем существующую запись напрямую, без reactivation-логики из
    # `_assign_if_missing`: если manual revoke сделан через UI — мы его уважаем.
    existing = await db.execute(
        select(UserRole).where(
            UserRole.user_id == user.id,
            UserRole.role_id == role.id,
        )
    )
    existing_assignment = existing.scalar_one_or_none()
    if existing_assignment is not None:
        # Запись уже есть — ничего не трогаем. Active останется active, revoked
        # останется revoked. Решение о реактивации принимается только в bootstrap
        # при рестарте бота.
        return False

    # Лог-идентификатор: telegram_id (публичный) или masked email (без PII).
    log_identifier = (
        str(user.telegram_id) if is_telegram_admin else _mask_email(user_email) if user_email else str(user.id)
    )
    # В AdminAuditLog кладём реальный identifier для forensics (БД защищена admin-only
    # доступом, в отличие от структlog'ов, которые уезжают в external aggregators).
    audit_identifier = str(user.telegram_id) if is_telegram_admin else (user_email or f'user_id={user.id}')

    # Savepoint: на IntegrityError откатывается ТОЛЬКО блок INSERT'а — outer
    # транзакция caller'а сохраняется. Раньше `await db.rollback()` сбрасывал
    # всю сессию, что было корректно только потому что в текущих callsite'ах
    # перед `ensure_superadmin_role_on_login` нет pending writes — но это
    # fragile инвариант, любой будущий рефактор мог его сломать.
    try:
        async with db.begin_nested():
            new_assignment = UserRole(
                user_id=user.id,
                role_id=role.id,
                is_active=True,
            )
            db.add(new_assignment)
            await db.flush()
    except IntegrityError:
        # Race: параллельный login или bootstrap уже создали запись.
        # Savepoint rolled back автоматически контекст-менеджером.
        return False

    # Audit log: env-driven Superadmin assignment должен быть видим в админ-UI,
    # не только в structlog. Пишем тоже в savepoint, чтобы fail аудита не сорвал
    # уже созданный grant.
    try:
        async with db.begin_nested():
            audit_entry = AdminAuditLog(
                user_id=user.id,
                action='rbac.superadmin.auto_grant_on_login',
                resource_type='user_role',
                resource_id=str(new_assignment.id),
                details={
                    'role_id': role.id,
                    'role_name': role.name,
                    'source': 'env',
                    'identifier': audit_identifier,
                    'matched_via': 'telegram_id' if is_telegram_admin else 'email',
                },
                status='success',
            )
            db.add(audit_entry)
            await db.flush()
    except Exception as audit_error:
        logger.warning(
            'Failed to write AdminAuditLog for Superadmin auto-grant',
            user_id=user.id,
            role_id=role.id,
            error=str(audit_error),
            exc_info=True,
        )

    logger.info(
        'Superadmin role assigned at login',
        user_id=user.id,
        role_id=role.id,
        identifier=log_identifier,
        matched_via='telegram_id' if is_telegram_admin else 'email',
    )
    return True


async def _ensure_role_by_email(
    db: AsyncSession,
    *,
    email: str,
    role_id: int,
) -> bool:
    """Assign Superadmin role to user found by verified email (NFKC + case-insensitive).

    SQL lookup делает `lower()` — поэтому ищем максимально широко, а NFKC-проверку
    делаем на Python-стороне после fetch'а. Так покрываются юзеры, в чьих email
    в БД присутствуют compatibility-символы (fullwidth и т.п.).
    """
    target = normalize_admin_email(email)
    result = await db.execute(
        select(User).where(
            func.lower(User.email) == email.lower(),
            User.email_verified.is_(True),
        )
    )
    candidates = result.scalars().all()
    user = next(
        (u for u in candidates if u.email is not None and normalize_admin_email(u.email) == target),
        None,
    )

    if user is None:
        logger.debug(
            'Admin user (email) not yet registered or not verified, skipping',
            email=email,
        )
        return False

    return await _assign_if_missing(db, user_id=user.id, role_id=role_id, identifier=email)


async def _assign_if_missing(
    db: AsyncSession,
    *,
    user_id: int,
    role_id: int,
    identifier: str,
) -> bool:
    """Create or reactivate a UserRole row for this user/role pair.

    Env config (ADMIN_IDS / ADMIN_EMAILS) is the source of truth for
    Superadmin assignments — за двумя исключениями:

    * Если предыдущий revoke был выполнен через UI (revocation_source='ui'),
      bootstrap НЕ реактивирует роль. Senior-админ сделал manual decision
      и его override должен оставаться в силе даже если юзер всё ещё в
      env config. Чтобы вернуть таких юзеров надо: либо
      переназначить роль вручную через cabinet UI (это обнуляет
      revocation_source), либо явно удалить строку из user_roles.

    * Для revocation_source IN (NULL, 'env') — реактивируем как раньше.
      NULL покрывает legacy строки до миграции 0078 (backward-compat).

    Returns True если запись была создана прямо сейчас (новая или
    реактивированная), False — если запись уже активна или manual UI revoke
    блокирует реактивацию.
    """
    result = await db.execute(
        select(UserRole).where(
            UserRole.user_id == user_id,
            UserRole.role_id == role_id,
        )
    )
    existing = result.scalar_one_or_none()

    if existing is not None:
        if existing.is_active:
            logger.debug(
                'User already has Superadmin role',
                user_id=user_id,
                identifier=identifier,
            )
            return False

        if existing.revocation_source == 'ui':
            logger.warning(
                'Skipping bootstrap reactivation: role was revoked via UI; env config will not override manual revoke',
                user_id=user_id,
                identifier=identifier,
                user_role_id=existing.id,
            )
            return False

        # Reactivate: env config is the source of truth (NULL=legacy treated as env-style).
        previous_revocation_source = existing.revocation_source
        existing.is_active = True
        existing.revocation_source = None
        await db.flush()
        logger.info(
            'Reactivated Superadmin role (user is in env config)',
            user_id=user_id,
            identifier=identifier,
            user_role_id=existing.id,
            previous_revocation_source=previous_revocation_source,
        )
        return True

    user_role = UserRole(
        user_id=user_id,
        role_id=role_id,
        is_active=True,
    )
    db.add(user_role)
    await db.flush()

    logger.info(
        'Assigned Superadmin role to user',
        user_id=user_id,
        role_id=role_id,
        identifier=identifier,
        user_role_id=user_role.id,
    )
    return True
