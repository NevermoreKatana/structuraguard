"""Небольшой закрытый ru/en concept dictionary; aliases не являются override."""

from decimal import Decimal

from ._names import Name, compare_names, token_dice
from ._scores import product

_CONCEPTS = (
    (("инн", "организации"), ("inn",)),
    (("tax", "id"), ("inn",)),
    (("общая", "сумма"), ("total", "amount")),
    (("дата", "создания"), ("created", "at")),
    (("номер", "заказа"), ("order", "number")),
    (("дата", "заказа"), ("ordered", "at")),
    (("название", "товара"), ("product", "name")),
    (("контрагент",), ("customer",)),
    (("покупатель",), ("customer",)),
    (("клиент",), ("customer",)),
    (("customers",), ("customer",)),
    (("клиенты",), ("customer",)),
    (("заказы",), ("order",)),
    (("orders",), ("order",)),
    (("products",), ("product",)),
    (("товары",), ("product",)),
    (("инн",), ("inn",)),
    (("сумма",), ("amount",)),
    (("цена",), ("price",)),
    (("количество",), ("quantity",)),
    (("дата",), ("date",)),
    (("статус",), ("status",)),
    (("телефон",), ("phone",)),
)


def concepts(tokens: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    i = 0
    while i < len(tokens):
        for source, target in _CONCEPTS:
            if tokens[i : i + len(source)] == source:
                result.extend(target)
                i += len(source)
                break
        else:
            result.append(tokens[i])
            i += 1
    return tuple(result)


def alias_score(
    sources: tuple[Name, ...], target: Name, aliases: tuple[Name, ...]
) -> Decimal:
    best = Decimal(0)
    for source in sources:
        for alias in aliases:
            evidence = compare_names(source, alias)
            value = max(
                evidence.exact,
                evidence.normalized,
                evidence.compact,
                min(Decimal("0.75"), evidence.transliterated),
            )
            best = max(best, value)
        a, b = concepts(source.tokens), concepts(target.tokens)
        if a != source.tokens or b != target.tokens:
            best = max(best, product(token_dice(a, b), Decimal("0.85")))
    return best


def context_similarity(source: Name, targets: tuple[Name, ...]) -> Decimal:
    return max(
        (
            max(compare_names(source, t).score, alias_score((source,), t, ()))
            for t in targets
        ),
        default=Decimal(0),
    )
