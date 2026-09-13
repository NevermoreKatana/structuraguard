"""Проекция policy в DB adapter limits; не открывает connections."""

from structuraguard.contracts.security import SecurityLimits
from structuraguard.database.target import InspectionLimits
from structuraguard.domain.resource_limits import llm_policy as llm_policy
from structuraguard.domain.resource_limits import load_policy as load_policy


def inspection_limits(
    limits: SecurityLimits, local: InspectionLimits
) -> InspectionLimits:
    """Сохранить локальные SQL/reflection caps и уменьшить общие maxima."""
    return InspectionLimits.model_validate(
        {
            **local.model_dump(warnings="error"),
            "max_columns": min(local.max_columns, limits.max_columns),
            "max_text_chars": min(local.max_text_chars, limits.max_text_chars),
            "timeout_seconds": min(
                local.timeout_seconds, limits.max_processing_time_ms / 1000
            ),
        }
    )
