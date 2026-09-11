"""Локальная PII evidence policy без raw samples и security approvals."""

from structuraguard.contracts.common import DataClassification
from structuraguard.contracts.profiling import (
    LocalePolicy,
    PIICategory,
    PIIClassificationRequest,
    PIIClassificationResult,
)
from structuraguard.profiling._patterns import scan_string


def classification_rank(value: DataClassification) -> int:
    """Вернуть ранг 0–3 для PUBLIC < INTERNAL < CONFIDENTIAL < RESTRICTED."""
    return (
        DataClassification.PUBLIC,
        DataClassification.INTERNAL,
        DataClassification.CONFIDENTIAL,
        DataClassification.RESTRICTED,
    ).index(value)


def maximum_classification(*values: DataClassification) -> DataClassification:
    """Выбрать наивысший класс; пустой набор даёт ValueError, I/O отсутствует."""
    return max(values, key=classification_rank)


class LocalPIIClassifier:
    """Классифицировать локальные PII-признаки без сети, LLM и разрешения egress.

    Анализирует отдельные имена/labels, категории значений и pattern counts.
    Отсутствие находок не доказывает отсутствие PII; это ограниченная эвристика.
    """

    async def classify(
        self, request: PIIClassificationRequest
    ) -> PIIClassificationResult:
        """Проверить request и классифицировать агрегированные признаки поля.

        Args:
            request: Поле, не более восьми labels, pattern/category evidence,
                checked/skipped counts, минимальный класс и input fingerprint.
                Request не содержит raw examples, но labels могут содержать PII.

        Returns:
            PIIClassificationResult с тем же field/input binding и классом
            не ниже minimum_classification. Находки дают detected; без находок
            полный непустой scan даёт not_detected, остальные случаи — unknown.
            complete означает checked_count > 0 и skipped_count == 0.

        Raises:
            ValueError: Некорректный request или несовпадение input fingerprint.

        Не выполняет I/O, не изменяет request и не создаёт SecurityApproval.
        Credentials повышают минимум до RESTRICTED, остальные находки —
        до CONFIDENTIAL. Полнота scan не является разрешением внешней отправки.
        """
        checked = PIIClassificationRequest.model_validate(
            request.model_dump(mode="python")
        )
        categories: set[PIICategory] = set(checked.value_categories)
        texts = (checked.field.field_name, *(label.text for label in checked.labels))
        # Склейка с именем поля разрушает full-string phone/INN/URL patterns.
        # Каждый bounded label сохраняет самостоятельную границу распознавания.
        for text in texts:
            categories.update(
                scan_string(text, LocalePolicy.UNSPECIFIED, money_hint=False).categories
            )
        names = " ".join(texts).casefold()
        if any(
            token in names
            for token in ("password", "secret", "api_key", "token", "пароль")
        ):
            categories.add("credential")
        if any(
            token in names
            for token in ("full_name", "first_name", "last_name", "фио", "фамил", "имя")
        ):
            categories.add("person_name")
        if any(token in names for token in ("address", "адрес")):
            categories.add("address")
        patterns = {item.code for item in checked.patterns if item.count}
        if "email" in patterns:
            categories.add("email")
        if "phone" in patterns:
            categories.add("phone")
        if "russian_inn_12" in patterns:
            categories.add("personal_tax_id")
        if "money" in patterns:
            categories.add("financial")
        classification = checked.minimum_classification
        if categories:
            classification = maximum_classification(
                classification, DataClassification.CONFIDENTIAL
            )
        if "credential" in categories:
            classification = DataClassification.RESTRICTED
        complete = checked.skipped_count == 0 and checked.checked_count > 0
        return PIIClassificationResult(
            field=checked.field,
            input_fingerprint=checked.input_fingerprint,
            state="detected"
            if categories
            else "not_detected"
            if complete
            else "unknown",
            categories=tuple(sorted(categories)),
            classification=classification,
            complete=complete,
        )
