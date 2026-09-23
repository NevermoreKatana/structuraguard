"""Лексика ru/en: ложное совпадение слабее явного имени или scoped alias."""

from decimal import Decimal, localcontext

import pytest
from hypothesis import given
from hypothesis import strategies as st

from structuraguard.mapping._aliases import alias_score
from structuraguard.mapping._names import compare_names, normalize_name


@pytest.mark.parametrize(
    "name",
    [
        "Customer Name",
        "customer-name",
        "CUSTOMERNAME",
        "customerName",
        "ＣＵＳＴＯＭＥＲ＿ＮＡＭＥ",
    ],
)
def test_normalized_forms_match_compact_name(name: str) -> None:
    evidence = compare_names(normalize_name(name), normalize_name("customer_name"))
    assert evidence.score >= Decimal("0.95")
    assert evidence.exact == 0


@pytest.mark.parametrize(
    ("source", "target"),
    [("Имя клиента", "imya_klienta"), ("Счёт", "schet"), ("Счет", "schet")],
)
def test_transliteration_is_capped(source: str, target: str) -> None:
    evidence = compare_names(normalize_name(source), normalize_name(target))
    assert evidence.transliterated == Decimal("0.80")
    assert evidence.score < Decimal("0.95")


def test_empty_forms_and_partial_substrings_are_not_exact_matches() -> None:
    assert compare_names(normalize_name("---"), normalize_name("___")).score == 0
    assert compare_names(normalize_name("id"), normalize_name("paid")).score == 0
    assert normalize_name("Клиeнт").confusable
    assert not normalize_name("Tax ИНН").confusable


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("postal_codde", "postal_code"),
        ("postla_code", "postal_code"),
        ("customer_adresss", "customer_address"),
        ("почтовый_инндекс", "почтовый_индекс"),
        ("διεύθυνσση", "διεύθυνση"),
    ],
)
def test_bounded_typos_offer_weaker_symmetric_name_evidence(
    source: str, target: str
) -> None:
    left, right = normalize_name(source), normalize_name(target)
    evidence = compare_names(left, right)
    assert Decimal("0.65") < evidence.edit < Decimal("0.90")
    assert evidence.edit == compare_names(right, left).edit
    assert evidence.score < compare_names(right, right).score
    assert (left.raw, right.raw) == (source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("id", "paid"),
        ("code", "codde"),
        ("sum", "sun"),
        ("email_1", "email_2"),
        ("email_10", "email_1"),
        ("email_0", "email"),
        ("Клиeнт", "Клиент"),
        ("weather", "legal_name"),
        ("postxxxxxxxxcode", "postal_code"),
    ],
)
def test_fuzzy_names_do_not_erase_meaningful_differences(
    source: str, target: str
) -> None:
    assert compare_names(normalize_name(source), normalize_name(target)).edit == 0


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("Общая сумма", "total_amount"),
        ("Дата создания", "created_at"),
        ("ИНН организации", "inn"),
        ("Tax ID", "inn"),
    ],
)
def test_builtin_aliases_are_bilingual(source: str, target: str) -> None:
    assert alias_score(
        (normalize_name(source),), normalize_name(target), ()
    ) == Decimal("0.85")
    assert alias_score((normalize_name(source),), normalize_name("unrelated"), ()) == 0


@given(st.text(alphabet="abcXYZ_-0123АБВабвё ", max_size=60))
def test_normalization_and_score_are_context_independent(text: str) -> None:
    name = normalize_name(text, max_tokens=64)
    expected = compare_names(name, normalize_name("customer_name"))
    with localcontext() as context:
        context.prec = 2
        assert normalize_name(text, max_tokens=64) == name
        assert compare_names(name, normalize_name("customer_name")) == expected
