from __future__ import annotations

import re
from pathlib import Path

from structuraguard.contracts import PipelineStatus

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
REQUIREMENTS_PATH = REPOSITORY_ROOT / "docs" / "requirements.md"
ARCHITECTURE_PATH = REPOSITORY_ROOT / "docs" / "architecture.md"
PUBLIC_API_PATH = REPOSITORY_ROOT / "docs" / "public-api.md"
ADR_PATH = REPOSITORY_ROOT / "docs" / "adr" / "0003-two-stage-parsing-contracts.md"
PIPELINE_FLOW = re.compile(
    r"`PIPE-001`[^\n]*\n\n```text\n(?P<pipeline>.*?)\n```",
    flags=re.DOTALL,
)
PIPELINE_STATUSES = re.compile(
    r"`PIPE-002`.*?```text\n(?P<statuses>[A-Z_\n]+)\n```",
    flags=re.DOTALL,
)
STALE_DIRECT_NORMALIZATION = re.compile(
    r"""
    \b(?:technical\s+)?parser\b
    (?![^.!?]{0,20}\bне\b)
    (?:
        \s*(?:-+>|→)\s*
        |\s+(?:(?:сразу|напрямую)\s+)?
            (?:созда(?:е|ё)т|формирует|возвращает)\s+
        |\s+преобразует(?:(?!extractedbatch)[^.!?]){0,80}\bв\s+
    )
    normalizedbatch\b
    (?![^.!?]{0,48}\b(?:запрещен\w*|неверн\w*|устарел\w*)\b)
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


def _normalize_text(value: str) -> str:
    return " ".join(value.replace("`", "").split())


def _normalized_text(path: Path) -> str:
    return _normalize_text(path.read_text(encoding="utf-8"))


def _requirement_text(requirement_id: str) -> str:
    requirements = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    match = re.search(
        rf"`{re.escape(requirement_id)}`(?P<body>.*?)(?=\n\n`[A-Z]+-\d+`|\Z)",
        requirements,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"{requirement_id} requirement block отсутствует")
    return _normalize_text(match.group("body"))


def test_documented_pipeline_statuses_match_the_public_enum() -> None:
    requirements = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    match = PIPELINE_STATUSES.search(requirements)

    assert match is not None, "PIPE-002 status block отсутствует"
    assert tuple(match.group("statuses").splitlines()) == tuple(
        status.value for status in PipelineStatus
    )


def test_canonical_docs_preserve_two_stage_parsing_and_llm_authority() -> None:
    requirements = _normalized_text(REQUIREMENTS_PATH)
    architecture = _normalized_text(ARCHITECTURE_PATH)
    public_api = _normalized_text(PUBLIC_API_PATH)
    adr = _normalized_text(ADR_PATH)
    canonical_docs = (
        requirements,
        architecture,
        public_api,
        adr,
    )
    for document in canonical_docs:
        assert "ExtractedBatch" in document
        assert "ParsePlan" in document
        assert "NormalizedBatch" in document

    requirements_source = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    pipeline_match = PIPELINE_FLOW.search(requirements_source)
    assert pipeline_match is not None, "PIPE-001 pipeline block отсутствует"
    pipeline = " ".join(pipeline_match.group("pipeline").split())
    ordered_pipeline_terms = (
        "technical parser",
        "ExtractedBatch",
        "structure profile/analyzer",
        "ParsePlan validation",
        "ParsePlan execution",
        "NormalizedBatch",
    )
    positions = tuple(pipeline.index(term) for term in ordered_pipeline_terms)
    assert positions == tuple(sorted(positions))

    parse_001 = _requirement_text("PARSE-001")
    parse_002 = _requirement_text("PARSE-002")
    parse_003 = _requirement_text("PARSE-003")
    llm_002 = _requirement_text("LLM-002")
    assert (
        "Technical parser MUST возвращать только physical ExtractedBatch" in parse_001
    )
    assert (
        "MUST NOT назначать окончательные semantic names, entities или DB targets"
        in parse_001
    )
    assert (
        "Semantic analyzer MUST формировать закрытый декларативный вариант ParsePlan"
        in parse_002
    )
    assert "Только применение ValidatedParsePlan создаёт NormalizedBatch" in parse_002
    assert "ParsePlan и MappingPlan являются разными contracts" in parse_003
    assert "Python, callbacks, shell и SQL в plans запрещены" in parse_003
    assert "LLM MAY предлагать schema-bound ParsePlan" in llm_002
    assert "не получает tools, source/DB handles, credentials" in llm_002
    assert "не создаёт исполняемый SQL" in llm_002
    assert "MUST пройти соответствующую deterministic plan validation" in llm_002

    assert (
        "technical parsing в ExtractedBatch без назначения бизнес-смысла"
        in architecture
    )
    assert "независимая проверка декларативного ParsePlan" in architecture
    assert (
        "Детерминированное применение ValidatedParsePlan и построение NormalizedBatch"
        in architecture
    )
    assert (
        "Она не получает DB connection, credentials, tools или право выполнять "
        "code/commands" in architecture
    )

    assert "Parser возвращает только raw physical structure" in public_api
    assert (
        "Semantic types появляются только после применения ValidatedParsePlan"
        in public_api
    )
    assert "DatabaseAdapter исполняет только ValidatedMappingPlan" in public_api
    assert "SQL не является частью публичного plan contract" in public_api
    assert "tools, credentials, handles, shell и SQL keys" in public_api

    assert "Parser выполняет только technical parsing" in adr
    assert "потоково возвращает ExtractedBatch" in adr
    assert "Только ValidatedParsePlan допускается в ParsePlanExecutor" in adr
    assert "ParsePlanExecutor детерминированно создаёт NormalizedBatch" in adr
    assert "SQL, physical selectors, callbacks и executable content" in adr
    assert "tools, credentials, source/DB handles или права на execution" in adr

    assert all(
        STALE_DIRECT_NORMALIZATION.search(document) is None
        for document in canonical_docs
    )


def test_stale_direct_normalization_detector_is_specific() -> None:
    stale_examples = (
        "Parser → NormalizedBatch",
        "technical parser -> NormalizedBatch",
        "Parser --> NormalizedBatch",
        "Parser создаёт NormalizedBatch",
        "Parser создает NormalizedBatch",
        "Parser формирует NormalizedBatch",
        "Parser напрямую возвращает NormalizedBatch",
        "Parser преобразует источник сразу в NormalizedBatch",
    )
    valid_examples = (
        "Parser возвращает только raw physical structure",
        "Parser не создаёт NormalizedBatch",
        "Parser --> ExtractedBatch --> ParsePlanExecutor --> NormalizedBatch",
        'Схема "Parser → NormalizedBatch" запрещена.',
    )

    assert all(STALE_DIRECT_NORMALIZATION.search(item) for item in stale_examples)
    assert all(
        STALE_DIRECT_NORMALIZATION.search(item) is None for item in valid_examples
    )
