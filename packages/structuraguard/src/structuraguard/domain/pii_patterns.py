"""Чистые bounded проверки identifiers без сетевой верификации."""

from structuraguard.contracts.profiling import PatternCode


def russian_inn(text: str) -> PatternCode | None:
    if (
        not text.isascii()
        or not text.isdigit()
        or len(text) not in (10, 12)
        or not int(text)
    ):
        return None
    digits = [int(c) for c in text]
    if len(digits) == 10:
        check = (
            sum(
                a * b
                for a, b in zip(digits, (2, 4, 10, 3, 5, 9, 4, 6, 8), strict=False)
            )
            % 11
            % 10
        )
        return "russian_inn_10" if check == digits[9] else None
    first = (
        sum(
            a * b for a, b in zip(digits, (7, 2, 4, 10, 3, 5, 9, 4, 6, 8), strict=False)
        )
        % 11
        % 10
    )
    second = (
        sum(
            a * b
            for a, b in zip(digits, (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8), strict=False)
        )
        % 11
        % 10
    )
    return "russian_inn_12" if (first, second) == (digits[10], digits[11]) else None


def card_like(text: str) -> bool:
    """ASCII 13–19 digits и Luhn; не проверяет банк, существование или владельца.

    Слишком длинный input отклоняется до обхода и int conversion. Число целиком
    не создаётся; однородные нули/цифры не считаются полезным card evidence.
    """
    if type(text) is not str or not 13 <= len(text) <= 37:
        return False
    if any(c not in "0123456789 -" for c in text):
        return False
    digits = [ord(c) - 48 for c in text if "0" <= c <= "9"]
    if not 13 <= len(digits) <= 19 or len(set(digits)) < 2:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        value = digit * 2 if index % 2 else digit
        total += value - 9 if value > 9 else value
    return total % 10 == 0
