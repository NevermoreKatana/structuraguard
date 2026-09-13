"""Проекция общих maxima в существующие LLM/DB contracts, без I/O."""

from structuraguard.contracts.constraint_validation import ConstraintReadPolicy
from structuraguard.contracts.llm import LLMBudget, LLMRoutingPolicy
from structuraguard.contracts.loading import PostgreSQLLoadPolicy
from structuraguard.contracts.security import SecurityLimits


def llm_policy(limits: SecurityLimits, policy: LLMRoutingPolicy) -> LLMRoutingPolicy:
    """Сузить budget до выпуска approval; destinations и privacy не меняются."""
    return LLMRoutingPolicy.model_validate(
        {
            **policy.model_dump(warnings="error"),
            "budget": LLMBudget(
                max_calls=min(policy.budget.max_calls, limits.max_llm_calls),
                max_tokens=min(policy.budget.max_tokens, limits.max_llm_tokens),
                max_time_ms=min(
                    policy.budget.max_time_ms,
                    limits.max_llm_time_ms,
                    limits.max_processing_time_ms,
                ),
            ),
        }
    )


def constraint_policy(
    limits: SecurityLimits, local: ConstraintReadPolicy
) -> ConstraintReadPolicy:
    """SELECT key batches используют прежние SQL builder и query guards."""
    return ConstraintReadPolicy.model_validate(
        {
            **local.model_dump(warnings="error"),
            "max_queries": min(local.max_queries, limits.max_db_queries),
            "chunk_size": min(local.chunk_size, limits.max_db_batch_rows),
        }
    )


def load_policy(
    limits: SecurityLimits, local: PostgreSQLLoadPolicy
) -> PostgreSQLLoadPolicy:
    """Ограничить batch до формирования parameter mappings/SQL statement."""
    data = local.model_dump(warnings="error")
    preflight = local.preflight.model_dump(warnings="error")
    preflight["read_policy"] = constraint_policy(limits, local.preflight.read_policy)
    return PostgreSQLLoadPolicy.model_validate(
        {
            **data,
            "preflight": preflight,
            "batch_size": min(local.batch_size, limits.max_db_batch_rows),
            "max_batch_bytes": min(local.max_batch_bytes, limits.max_db_batch_bytes),
        }
    )
