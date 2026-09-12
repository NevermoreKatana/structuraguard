"""Настоящие parser/ParsePlan artifacts для проверки provenance без I/O."""

from dataclasses import dataclass

from structuraguard.contracts.normalized import (
    NormalizedBatch,
    NormalizedDatasetManifest,
    NormalizedValue,
)
from structuraguard.contracts.parsing import ParseExecutionContext, ValidatedParsePlan
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.parsers.builtin import HtmlParser, JsonDocumentParser, XmlParser
from structuraguard.ports.parser import Parser
from structuraguard.structure import ParsePlanExecutor, ParsePlanValidator
from tests.unit.structure.test_execution import execution_context, prepared, stream


@dataclass(frozen=True)
class ProvenanceFixture:
    physical: tuple[ExtractedBatch, ...]
    normalized: tuple[NormalizedBatch, ...]
    plan: ValidatedParsePlan
    context: ParseExecutionContext


async def provenance_fixture(
    content: bytes = b'[{"name":" Ada "},{"name":"Bob"}]',
    *,
    parser: Parser | None = None,
) -> ProvenanceFixture:
    if isinstance(parser, (XmlParser, HtmlParser)):
        from tests.unit.structure.test_explicit_markup_plans import (
            explicit_markup_request,
        )

        request, physical = await explicit_markup_request(parser, content)
    else:
        request, physical = await prepared(
            parser if parser is not None else JsonDocumentParser(),
            content,
            batch_size=1,
        )
    validated = await ParsePlanValidator().validate_source(request, stream(physical))
    assert validated.validated_plan is not None
    context = execution_context(request)
    normalized = tuple(
        [
            item
            async for item in ParsePlanExecutor().execute(
                stream(physical), validated.validated_plan, context
            )
        ]
    )
    return ProvenanceFixture(physical, normalized, validated.validated_plan, context)


def rehash(batches: tuple[NormalizedBatch, ...]) -> tuple[NormalizedBatch, ...]:
    """Пересчитать hashes поддельного, но структурно допустимого artifact."""
    manifest = batches[-1].manifest
    assert manifest is not None
    result: list[NormalizedBatch] = []
    for batch in batches:
        data = batch.model_dump(mode="python")
        data.update(
            batch_fingerprint="sha256:" + "0" * 64, is_last=False, manifest=None
        )
        result.append(NormalizedBatch.model_validate(data))
    data = manifest.model_dump(mode="python")
    data.update(
        normalized_fingerprint="sha256:" + "0" * 64,
        source=result[0].source,
        batches=tuple(batch.to_summary() for batch in result),
    )
    rebuilt = NormalizedDatasetManifest.model_validate(data)
    data = result[-1].model_dump(mode="python")
    data.update(is_last=True, manifest=rebuilt, batch_fingerprint="sha256:" + "0" * 64)
    # is_last участвует в hash, поэтому summary терминального batch также меняется.
    terminal = batches[-1].model_dump(mode="python")
    terminal.update(batch_fingerprint="sha256:" + "0" * 64, manifest=rebuilt)
    # Строить terminal hash отдельно от manifest, чтобы избежать circular binding.
    from structuraguard.contracts._base import canonical_sha256_value

    terminal["batch_fingerprint"] = canonical_sha256_value(
        terminal, exclude_top_level=frozenset({"batch_fingerprint", "manifest"})
    )
    draft = result[-1].model_copy(
        update={"is_last": True, "batch_fingerprint": terminal["batch_fingerprint"]}
    )
    data = rebuilt.model_dump(mode="python")
    data.update(
        normalized_fingerprint="sha256:" + "0" * 64,
        batches=tuple(b.to_summary() for b in (*result[:-1], draft)),
    )
    terminal["manifest"] = NormalizedDatasetManifest.model_validate(data)
    result[-1] = NormalizedBatch.model_validate(terminal)
    return tuple(result)


def replace_value(
    fixture: ProvenanceFixture, value: NormalizedValue
) -> tuple[NormalizedBatch, ...]:
    """Заменить только первое value и пересчитать все hashes."""
    batch = fixture.normalized[0]
    record = batch.records[0]
    entity = record.entities[0]
    altered = batch.model_copy(
        update={
            "records": (
                record.model_copy(
                    update={
                        "entities": (
                            entity.model_copy(
                                update={"values": (value, *entity.values[1:])}
                            ),
                            *record.entities[1:],
                        )
                    }
                ),
                *batch.records[1:],
            )
        }
    )
    return rehash((altered, *fixture.normalized[1:]))
