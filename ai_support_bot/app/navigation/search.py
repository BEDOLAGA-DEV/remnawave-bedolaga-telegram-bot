from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ai_support_bot.app.navigation.schema import NavNode, NavTree


_TOKEN_RE = re.compile(r'[0-9a-zA-Zа-яёА-ЯЁ]+')

_STOPWORDS = frozenset(
    {
        'а', 'бы', 'в', 'вот', 'все', 'всё', 'где', 'да', 'для', 'до', 'если', 'есть', 'ещё', 'еще',
        'же', 'за', 'и', 'из', 'или', 'как', 'кто', 'ли', 'мне', 'мной', 'мой', 'моя', 'мои', 'на',
        'над', 'не', 'нет', 'но', 'о', 'об', 'от', 'по', 'под', 'при', 'про', 'с', 'со', 'та', 'так',
        'такое', 'там', 'то', 'тут', 'ты', 'у', 'уже', 'что', 'чтобы', 'это', 'этот', 'я',
        'подскажите', 'скажите', 'пожалуйста', 'помогите', 'хочу', 'нужно', 'надо', 'можно', 'можете',
        'найти', 'находится', 'находиться', 'сделать',
        'a', 'an', 'and', 'are', 'be', 'can', 'do', 'for', 'how', 'i', 'in', 'is', 'it', 'me', 'my',
        'of', 'on', 'or', 'please', 'the', 'to', 'want', 'what', 'where', 'you', 'your',
    }
)

_SYNONYMS: dict[str, tuple[str, ...]] = {
    'вывести': ('вывод', 'выплата'),
    'вывод': ('вывод', 'выплата'),
    'выводить': ('вывод', 'выплата'),
    'выплату': ('вывод', 'выплата'),
    'выплата': ('вывод', 'выплата'),
    'снять': ('вывод', 'выплата'),
    'обналичить': ('вывод', 'выплата'),
    'партнерка': ('партнер', 'реферал'),
    'партнерская': ('партнер', 'реферал'),
    'рефералка': ('реферал', 'партнер'),
    'реф': ('реферал',),
    'бонус': ('бонус', 'реферал'),
    'деньги': ('баланс',),
    'пополнить': ('пополнение', 'баланс'),
    'пополнение': ('пополнение', 'баланс'),
    'оплатить': ('оплата', 'пополнение'),
    'платеж': ('оплата',),
    'платёж': ('оплата',),
    'тариф': ('тариф', 'подписка'),
    'гб': ('трафик',),
    'девайс': ('устройство',),
    'девайсы': ('устройство',),
    'ключ': ('ссылка', 'подключение'),
    'конфиг': ('ссылка', 'подключение'),
    'подключить': ('подключение', 'подключиться'),
    'подключение': ('подключение', 'подключиться'),
    'промик': ('промокод',),
    'скидка': ('скидка', 'промогруппа'),
    'тикет': ('тикет', 'обращение', 'поддержка'),
    'оператор': ('поддержка', 'оператор'),
    'саппорт': ('поддержка',),
    'продлить': ('продление',),
    'автоплатеж': ('автоплатеж', 'автопродление'),
    'автоплатёж': ('автоплатеж', 'автопродление'),
    'withdraw': ('вывод', 'выплата'),
    'withdrawal': ('вывод', 'выплата'),
    'payout': ('вывод', 'выплата'),
    'referral': ('реферал', 'партнер'),
    'balance': ('баланс',),
    'topup': ('пополнение', 'баланс'),
    'subscription': ('подписка',),
    'traffic': ('трафик',),
    'devices': ('устройство',),
    'support': ('поддержка',),
    'promocode': ('промокод',),
}

_TITLE_WEIGHT = 6.0
_LABEL_WEIGHT = 5.0
_KEYWORD_WEIGHT = 4.0
_HINT_WEIGHT = 1.5
_PHRASE_BONUS = 5.0
_CHILD_MATCH_WEIGHT = 0.4
_DEPTH_BONUS = 0.14
_RELATIVE_CUTOFF = 0.45

_SUFFIXES = (
    'ированием', 'ированию', 'ования', 'овании', 'ами', 'ями', 'ого', 'ему', 'ому',
    'ых', 'их', 'ов', 'ев', 'ам', 'ям', 'ах', 'ях', 'ой', 'ей', 'ую', 'юю', 'ые',
    'ие', 'ий', 'ый', 'ая', 'яя', 'ое', 'ее', 'ть', 'ла', 'ло', 'ли', 'ем', 'им',
    'ing', 'ers', 'er', 'es', 'у', 'ю', 'а', 'я', 'ы', 'и', 'е', 'о', 'ь', 'й', 's',
)


@dataclass(slots=True)
class NavMatch:
    node: NavNode
    score: float


def _fold(value: str) -> str:
    normalized = unicodedata.normalize('NFKD', value or '')
    stripped = ''.join(char for char in normalized if not unicodedata.combining(char))
    return stripped.replace('ё', 'е').replace('Ё', 'Е').lower()


def _stem(token: str) -> str:
    if len(token) <= 4:
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def tokenize(value: str) -> list[str]:
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(_fold(value)):
        if len(raw) < 2 or raw in _STOPWORDS:
            continue
        tokens.append(raw)
        tokens.extend(_fold(item) for item in _SYNONYMS.get(raw, ()))
    return tokens


def _stems(tokens: list[str]) -> set[str]:
    return {_stem(token) for token in tokens if token}


def _field_score(query_stems: set[str], value: str, weight: float) -> float:
    if not value or not query_stems:
        return 0.0
    field_stems = _stems(tokenize(value))
    if not field_stems:
        return 0.0
    hits = len(query_stems & field_stems)
    if not hits:
        return 0.0
    coverage = hits / len(query_stems)
    density = hits / len(field_stems)
    return weight * (coverage + 0.5 * density)


def _node_score(node: NavNode, folded_query: str, query_stems: set[str]) -> float:
    score = 0.0
    score += _field_score(query_stems, node.title, _TITLE_WEIGHT)
    score += _field_score(query_stems, node.bot_label or '', _LABEL_WEIGHT)
    score += _field_score(query_stems, node.web_label or '', _LABEL_WEIGHT * 0.6)
    score += _field_score(query_stems, ' '.join(node.keywords), _KEYWORD_WEIGHT)
    score += _field_score(query_stems, node.hint, _HINT_WEIGHT)

    for candidate in (node.title, node.bot_label or '', node.web_label or '', *node.keywords):
        folded = _fold(candidate)
        if len(folded) >= 5 and folded in folded_query:
            score += _PHRASE_BONUS
            break
    return score


def search(tree: NavTree, query: str, limit: int = 3) -> list[NavMatch]:
    query_stems = _stems(tokenize(query))
    if not query_stems:
        return []

    folded_query = _fold(query)
    direct: dict[str, float] = {}
    for node in tree.iter_nodes():
        value = _node_score(node, folded_query, query_stems)
        if value > 0:
            direct[node.id] = value

    if not direct:
        return []

    aggregated: dict[str, float] = {}
    for node_id, value in direct.items():
        node = tree.index.get(node_id)
        if node is None:
            continue
        ancestors = tree.ancestors(node)
        aggregated[node_id] = aggregated.get(node_id, 0.0) + value * (1.0 + _DEPTH_BONUS * len(ancestors))
        for ancestor in ancestors:
            aggregated[ancestor.id] = aggregated.get(ancestor.id, 0.0) + value * _CHILD_MATCH_WEIGHT

    ranked = sorted(
        aggregated.items(),
        key=lambda item: (
            -direct.get(item[0], 0.0),
            -item[1],
            -len(tree.ancestors(tree.index[item[0]])) if item[0] in tree.index else 0,
            item[0],
        ),
    )
    if not ranked:
        return []

    best_score = max(direct.get(node_id, 0.0) for node_id, _ in ranked)
    if best_score <= 0:
        best_score = ranked[0][1]

    matches: list[NavMatch] = []
    chosen: set[str] = set()
    root_ids = {node.id for node in tree.roots}
    for node_id, value in ranked:
        node_direct = direct.get(node_id, 0.0)
        if node_direct <= 0 and value < best_score * _RELATIVE_CUTOFF:
            break
        node = tree.index.get(node_id)
        if node is None:
            continue
        if node_id in root_ids and len(ranked) > 1:
            continue
        if any(ancestor.id in chosen for ancestor in tree.ancestors(node)):
            continue
        if any(node_id in {anc.id for anc in tree.ancestors(tree.index[c_id])} for c_id in chosen if c_id in tree.index):
            continue
        chosen.add(node_id)
        matches.append(NavMatch(node=node, score=round(max(node_direct, value), 3)))
        if len(matches) >= max(1, limit):
            break
    return matches
