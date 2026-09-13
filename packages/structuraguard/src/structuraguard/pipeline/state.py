"""Разрешённые edges pipeline; числовой порядок enum не является policy."""

from structuraguard.contracts.common import PipelineStatus as S
from structuraguard.exceptions import StructuraGuardError

TERMINAL = frozenset(
    (
        S.COMPLETED,
        S.COMPLETED_WITH_WARNINGS,
        S.NEEDS_REVIEW,
        S.REJECTED_SECURITY,
        S.ROLLED_BACK,
        S.FAILED,
        S.CANCELLED,
    )
)
EDGES = (
    (S.CREATED, (S.SOURCE_PROBING, S.DATABASE_INSPECTING)),
    (S.SOURCE_PROBING, (S.TECHNICAL_PARSING,)),
    (S.TECHNICAL_PARSING, (S.STRUCTURE_PROFILING,)),
    (S.STRUCTURE_PROFILING, (S.STRUCTURE_ANALYZING,)),
    (S.STRUCTURE_ANALYZING, (S.PARSE_PLAN_CREATED,)),
    (S.PARSE_PLAN_CREATED, (S.PARSE_PLAN_VALIDATING,)),
    (S.PARSE_PLAN_VALIDATING, (S.SEMANTIC_PARSING,)),
    (S.SEMANTIC_PARSING, (S.NORMALIZED_DATA_PROFILING,)),
    (S.NORMALIZED_DATA_PROFILING, (S.DATABASE_INSPECTING,)),
    (S.DATABASE_INSPECTING, (S.MAPPING, S.MAPPING_PLAN_CREATED)),
    (S.MAPPING, (S.MAPPING_PLAN_CREATED,)),
    (S.MAPPING_PLAN_CREATED, (S.MAPPING_PLAN_VALIDATING,)),
    (S.MAPPING_PLAN_VALIDATING, (S.NORMALIZING,)),
    (S.NORMALIZING, (S.VALIDATING,)),
    (S.VALIDATING, (S.STAGING,)),
    (S.STAGING, (S.LOADING,)),
    (S.LOADING, ()),
)


def check_transition(current: S, following: S) -> None:
    """Повтор stage и выход из terminal запрещены; retry не меняет status."""
    allowed = next((targets for source, targets in EDGES if source is current), ())
    if current in TERMINAL or (following not in allowed and following not in TERMINAL):
        raise StructuraGuardError(
            error_code="SDK_INVALID_TRANSITION", message="SDK_INVALID_TRANSITION"
        )
