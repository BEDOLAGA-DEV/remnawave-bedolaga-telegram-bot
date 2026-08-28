"""Канонический вид почтового адреса — против регистраций на алиасах одного ящика.

Проверка «email уже занят» сравнивала адреса с точностью до регистра, а почтовые
провайдеры доставляют письма в один ящик по целому семейству адресов:

    user+1@gmail.com, user+2@gmail.com, u.s.e.r@gmail.com  →  user@gmail.com

Для владельца ящика это один почтовый ящик, для базы — сколько угодно разных
пользователей. Там, где к регистрации привязана выдача чего-либо разового
(пробная подписка, приветственный бонус, промокод), получатель ограничен только
терпением: подтверждение адреса проходит штатно, письмо приходит ему же.

Канонизация умышленно консервативная — склеить двух разных людей хуже, чем
пропустить одного лишнего:

* точки в локальной части игнорирует только Gmail, поэтому убираем их лишь для
  ``gmail.com`` и ``googlemail.com``;
* субадресацию (``+suffix``, RFC 5233) поддерживают не все, поэтому режем её
  только у провайдеров из списка ниже. Корпоративные и незнакомые домены
  остаются как есть: там ``+`` вполне может быть частью имени ящика.

Сам адрес пользователя не переписывается: письма должны уходить ровно на тот
адрес, который человек ввёл. Канонический вид нужен только для сравнения.
"""

from __future__ import annotations

from sqlalchemy import func, or_


# Провайдеры, у которых всё после «+» — пользовательская метка, а не часть адреса
PLUS_ADDRESSING_DOMAINS = frozenset(
    {
        'gmail.com',
        'googlemail.com',
        'outlook.com',
        'hotmail.com',
        'live.com',
        'icloud.com',
        'me.com',
        'yandex.ru',
        'yandex.com',
        'ya.ru',
        'mail.ru',
        'proton.me',
        'protonmail.com',
        'fastmail.com',
    }
)

# Домены, игнорирующие точки в локальной части
DOT_INSENSITIVE_DOMAINS = frozenset({'gmail.com', 'googlemail.com'})

# Провайдеры, у которых несколько доменов ведут в один ящик
DOMAIN_ALIASES = {
    'googlemail.com': 'gmail.com',
    'ya.ru': 'yandex.ru',
    'yandex.com': 'yandex.ru',
    'protonmail.com': 'proton.me',
    'me.com': 'icloud.com',
}


def _canonical_parts(email: str | None) -> tuple[str, str] | None:
    """Канонические (локальная часть, домен) или ``None``, если канонизировать нечего.

    Единственное место, где живёт порядок преобразований: сначала отрезается
    субадрес, потом убираются точки. Иначе «u.s+ta.g@gmail.com» свернулся бы
    по-разному в python и в SQL.
    """
    normalized = (email or '').strip().lower()
    if normalized.count('@') != 1:
        return None

    local, domain = normalized.split('@', 1)
    domain = DOMAIN_ALIASES.get(domain, domain)

    if domain in PLUS_ADDRESSING_DOMAINS:
        local = local.split('+', 1)[0]
    if domain in DOT_INSENSITIVE_DOMAINS:
        local = local.replace('.', '')

    if not local:
        # «+tag@gmail.com» и подобное — локальной части не осталось, сравнивать
        # такое с чем-либо нельзя
        return None

    return local, domain


def canonical_email(email: str | None) -> str:
    """Вид адреса для сравнения: тот же ящик — та же строка.

    Адрес, который не поддаётся канонизации (без «@», пустой, без локальной
    части), возвращается просто в нижнем регистре: спорные входные данные лучше
    не трогать, их отвергнет валидация выше.
    """
    parts = _canonical_parts(email)
    if parts is None:
        return (email or '').strip().lower()
    local, domain = parts
    return f'{local}@{domain}'


def is_email_alias_of(candidate: str | None, existing: str | None) -> bool:
    """Оба адреса ведут в один ящик, но записаны по-разному."""
    if not candidate or not existing:
        return False
    if candidate.strip().lower() == existing.strip().lower():
        return False
    return canonical_email(candidate) == canonical_email(existing)


def email_domain(email: str | None) -> str:
    """Домен адреса с учётом слияния доменов-близнецов (ya.ru → yandex.ru)."""
    normalized = (email or '').strip().lower()
    if normalized.count('@') != 1:
        return ''
    domain = normalized.split('@', 1)[1]
    return DOMAIN_ALIASES.get(domain, domain)


def has_alias_forms(email: str | None) -> bool:
    """У этого адреса вообще бывают алиасы — есть ли смысл искать двойников."""
    domain = email_domain(email)
    return domain in PLUS_ADDRESSING_DOMAINS or domain in DOT_INSENSITIVE_DOMAINS


def sibling_domains(domain: str) -> set[str]:
    """Домены, письма с которых попадают в тот же ящик, включая сам домен."""
    if not domain:
        return set()
    return {domain} | {src for src, dst in DOMAIN_ALIASES.items() if dst == domain}


_LIKE_ESCAPE = '\\'


def _escape_like(value: str) -> str:
    """Экранирование для LIKE: в локальной части легко встречается «_»."""
    for char in (_LIKE_ESCAPE, '%', '_'):
        value = value.replace(char, _LIKE_ESCAPE + char)
    return value


def alias_match_clause(column, email: str | None):
    """SQLAlchemy-условие: колонка хранит адрес того же ящика, что и ``email``.

    Считается на стороне БД, чтобы не вычитывать всех пользователей домена ради
    одной регистрации, и намеренно обходится только ``lower``/``replace``/``LIKE``:
    ``split_part`` есть в PostgreSQL, но не в SQLite, а он здесь полноценный
    режим работы (``DATABASE_MODE=sqlite``, и ``auto`` скатывается в него без
    настроенного PostgreSQL).

    Условие точное, а не приблизительное. После тех же преобразований, что и в
    ``canonical_email``, хранимый адрес либо совпадает с каноническим целиком,
    либо отличается ровно субадресом — то есть начинается с ``канон + '+'``.
    Разбор адреса общий с ``canonical_email`` — см. ``_canonical_parts``.
    """
    parts = _canonical_parts(email)
    if parts is None:
        return None
    local, domain = parts

    strips_dots = domain in DOT_INSENSITIVE_DOMAINS
    strips_subaddress = domain in PLUS_ADDRESSING_DOMAINS
    if not (strips_dots or strips_subaddress):
        # У домена нет алиасов — искать нечего, а точный дубль и так ловится
        # обычным поиском по адресу. Лишний запрос на каждую регистрацию не нужен.
        return None

    expr = func.lower(column)
    if strips_dots:
        # Точки уходят и из домена тоже — сравниваемая сторона строится так же
        expr = func.replace(expr, '.', '')

    clauses = []
    for sibling in sorted(sibling_domains(domain)):
        stored_domain = sibling.replace('.', '') if strips_dots else sibling
        clauses.append(expr == f'{local}@{stored_domain}')
        if strips_subaddress:
            pattern = f'{_escape_like(local)}+%@{_escape_like(stored_domain)}'
            clauses.append(expr.like(pattern, escape=_LIKE_ESCAPE))

    return or_(*clauses)
