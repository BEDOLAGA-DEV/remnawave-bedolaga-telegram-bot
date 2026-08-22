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


def canonical_email(email: str | None) -> str:
    """Вид адреса для сравнения: тот же ящик — та же строка.

    Адрес без «@» (или пустой) возвращается просто в нижнем регистре: спорные
    входные данные лучше не трогать, их отвергнет валидация выше.
    """
    normalized = (email or '').strip().lower()
    if normalized.count('@') != 1:
        return normalized

    local, domain = normalized.split('@', 1)
    domain = DOMAIN_ALIASES.get(domain, domain)

    if domain in PLUS_ADDRESSING_DOMAINS:
        local = local.split('+', 1)[0]
    if domain in DOT_INSENSITIVE_DOMAINS:
        local = local.replace('.', '')

    if not local:
        # «+tag@gmail.com» и подобное — локальной части не осталось, сравнивать
        # такое с чем-либо нельзя, отдаём исходный адрес
        return normalized

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


def canonical_local_sql(column, domain: str):
    """SQL-выражение канонической локальной части — зеркало ``canonical_email``.

    Считается на стороне БД, чтобы не вычитывать всех пользователей домена ради
    одной регистрации. Порядок операций обязан совпадать с python-версией:
    сначала отрезаем субадрес, потом убираем точки — иначе «u.s+ta.g@gmail.com»
    свернётся здесь и там по-разному.
    """
    from sqlalchemy import func

    expr = func.split_part(func.lower(column), '@', 1)
    if domain in PLUS_ADDRESSING_DOMAINS:
        expr = func.split_part(expr, '+', 1)
    if domain in DOT_INSENSITIVE_DOMAINS:
        expr = func.replace(expr, '.', '')
    return expr
