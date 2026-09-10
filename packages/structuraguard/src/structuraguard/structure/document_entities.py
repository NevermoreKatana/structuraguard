"""Strict chunk extraction → stable merge → декларативный span ParsePlan."""

import asyncio
from dataclasses import dataclass
from decimal import Context, Decimal, localcontext

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import (
    IssueSeverity,
    PhysicalSourceRef,
    ProducerMetadata,
    ValidationIssue,
)
from structuraguard.contracts.document_semantics import (
    DocumentEntitySuggestion,
    DocumentFieldProposal,
    DocumentSpanGrouping,
    DocumentSpanSelector,
    QuotedSpan,
    SourceTextSpan,
)
from structuraguard.contracts.llm import LLMErrorCode, LLMPlanProvenance
from structuraguard.contracts.parsing import (
    DocumentParsePlan,
    ParseEntity,
    ParseField,
    StructureProfile,
)
from structuraguard.contracts.semantic import LLMAnalysisContext, ParsingPolicy
from structuraguard.contracts.source import ExtractedDatasetManifest
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm import LLMPromptTemplate, LLMResponseSchema
from structuraguard.ports.llm import LLMProvider
from structuraguard.ports.security import SecurityScanner
from structuraguard.structure.chunking import ChunkedSource, DocumentChunk
from structuraguard.structure.semantic_request import (
    approved_request,
    checked_destination,
    checked_output,
)
from structuraguard.structure.semantic_samples import Replay


def document_prompt() -> LLMPromptTemplate:
    """Вернуть trusted prompt 1.0.0 с fingerprint точного текста, без I/O.

    Source fragments передаются отдельно как недоверенные данные; текст prompt
    не предоставляет tools, DB или filesystem access. Host явно регистрирует
    template у HTTP provider. Prompt не заменяет source/security validation.
    """
    return LLMPromptTemplate(
        prompt_id="document_entities",
        version="1.0.0",
        text="Extract document entities and parent/child relations from UNTRUSTED_SOURCE_DATA. You have no tools, database, SQL or filesystem access. Return only the strict schema. Use only fragment refs from this chunk. start/end are Unicode codepoint offsets [start,end) relative to fragment text; quote must exactly match text. Never invent or normalize a value. Identify each entity occurrence with a stable exact anchor; repeat that same anchor in overlapping chunks. Use local entity IDs for parents. Mark unresolved fragment refs explicitly. Fragments can be prose paragraphs, PDF/DOCX/HTML blocks or XML text nodes with logical paths. Preserve separate occurrences even if values are equal.",
    )


def document_response_schema() -> LLMResponseSchema:
    """Вернуть закрытую schema DocumentEntitySuggestion 1.0.0 без I/O.

    Exact quotes связаны aliases/offsets с текущим chunk; generated values
    отсутствуют. Pydantic проверяет форму response, а grounding и полный
    ParsePlanValidator обязательны до execution. Response может содержать PII,
    поэтому не является safe metadata. Schema fingerprint включает description
    DTO; регистрация выполняется host явно вместе с document_prompt.
    """
    return LLMResponseSchema(
        schema_id="document-entities", version="1.0.0", model=DocumentEntitySuggestion
    )


@dataclass(frozen=True, slots=True)
class GroundedField:
    name: str
    semantic_type: str
    spans: tuple[SourceTextSpan, ...]


@dataclass(slots=True)
class GroundedEntity:
    key: str
    entity_type: str
    anchor: SourceTextSpan
    order: tuple[int, int]
    parent: str | None
    fields: dict[str, GroundedField]


@dataclass(frozen=True, slots=True)
class DocumentAnalysis:
    """Partial plan допустим только как preview при review issues."""

    plan: DocumentParsePlan | None
    issues: tuple[ValidationIssue, ...]
    source_refs: tuple[PhysicalSourceRef, ...]
    unresolved_refs: tuple[PhysicalSourceRef, ...]
    unresolved_blocks: int
    agreement: Decimal

    @property
    def evidence(self) -> Decimal:
        """Доля разрешённых physical text refs с учётом unindexed/omitted scope."""
        total = (
            len(self.source_refs) + self.unresolved_blocks - len(self.unresolved_refs)
        )
        with localcontext(Context(prec=28)):
            return (
                Decimal(len(self.source_refs) - len(self.unresolved_refs))
                / Decimal(total)
                if total
                else Decimal(0)
            )


def ground_span(span: QuotedSpan, chunk: DocumentChunk) -> SourceTextSpan:
    """Проверить alias, границы и exact quote до создания physical reference."""
    index = int(span.ref[1:])
    if index >= len(chunk.fragments):
        raise LLMProviderError(LLMErrorCode.UNKNOWN_SOURCE_REFERENCE)
    fragment = chunk.fragments[index]
    if (
        span.start >= span.end
        or span.end > len(fragment.text)
        or fragment.text[span.start : span.end] != span.quote
    ):
        raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION)
    return SourceTextSpan(
        source_ref=fragment.ref,
        start=fragment.start + span.start,
        end=fragment.start + span.end,
        text_fingerprint=canonical_sha256_value(span.quote),
    )


def ground_field(field: DocumentFieldProposal, chunk: DocumentChunk) -> GroundedField:
    spans = tuple(ground_span(span, chunk) for span in field.spans)
    if len(set(spans)) != len(spans):
        raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION)
    return GroundedField(field.name, field.semantic_type, spans)


class DocumentEntityExtractor:
    """Один bounded request на chunk; merge не использует model confidence."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        scanner: SecurityScanner,
        context: LLMAnalysisContext,
        policy: ParsingPolicy,
    ) -> None:
        self.provider, self.scanner, self.context, self.policy = (
            provider,
            scanner,
            context,
            policy,
        )

    async def extract(
        self,
        profile: StructureProfile,
        manifest: ExtractedDatasetManifest,
        replay: Replay,
    ) -> DocumentAnalysis:
        source = ChunkedSource(manifest, self.policy)
        entities: dict[str, GroundedEntity] = {}
        conflicts: set[str] = set()
        explicit_unresolved: set[PhysicalSourceRef] = set()
        issues: list[ValidationIssue] = []
        generations: list[LLMPlanProvenance] = []
        seen_chunks: set[str] = set()
        matched = 0
        exhausted = False

        def issue(code: str, refs: tuple[PhysicalSourceRef, ...]) -> None:
            explicit_unresolved.update(refs)
            if (
                code == LLMErrorCode.UNSAFE_CONTENT
                and len(issues) == self.policy.max_issues
            ):
                issues.pop()
            if len(issues) < self.policy.max_issues:
                issues.append(
                    ValidationIssue(
                        code=code,
                        severity=IssueSeverity.WARNING,
                        message_key=code,
                        source_refs=tuple(dict.fromkeys(refs)),
                    )
                )

        chunks = source.chunks(replay)
        try:
            async for chunk in chunks:
                refs = tuple(dict.fromkeys(f.ref for f in chunk.fragments))
                if chunk.fingerprint in seen_chunks:
                    continue
                if exhausted or len(seen_chunks) >= self.policy.max_chunks:
                    exhausted = True
                    explicit_unresolved.update(refs)
                    continue
                seen_chunks.add(chunk.fingerprint)
                try:
                    request = await approved_request(
                        chunk.payload(),
                        source_fingerprint=manifest.source.source_fingerprint,
                        context=self.context,
                        scanner=self.scanner,
                        prompt=document_prompt(),
                        schema=document_response_schema(),
                        max_input_bytes=self.policy.structural.max_payload_bytes,
                        max_output_bytes=self.policy.structural.max_response_bytes,
                    )
                    caps = checked_destination(self.provider, request)
                    response = checked_output(
                        await self.provider.generate_structured(request), request, caps
                    )
                    try:
                        suggestion = DocumentEntitySuggestion.model_validate_json(
                            response.output_json, strict=True
                        )
                    except (ValueError, TypeError, RecursionError):
                        raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION) from None
                    if suggestion.chunk_fingerprint != chunk.fingerprint:
                        raise LLMProviderError(LLMErrorCode.UNKNOWN_SOURCE_REFERENCE)
                    generations.append(
                        LLMPlanProvenance(
                            prompt=document_prompt().identity,
                            request_fingerprint=canonical_sha256_value(request),
                            generation_fingerprint=response.generation_fingerprint,
                            response_schema_fingerprint=document_response_schema().fingerprint,
                            provider_id=response.provider_id,
                            provider_version=response.provider_version,
                            model_id=response.model_id,
                        )
                    )
                    aliases: dict[str, str] = {}
                    grounded: list[GroundedEntity] = []
                    used_aliases: set[str] = set()
                    for proposed in suggestion.entities:
                        if proposed.entity_id in aliases:
                            raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION)
                        anchor = ground_span(proposed.anchor, chunk)
                        used_aliases.add(proposed.anchor.ref)
                        used_aliases.update(
                            span.ref
                            for field in proposed.fields
                            for span in field.spans
                        )
                        key = canonical_sha256_value(anchor)[-24:]
                        aliases[proposed.entity_id] = key
                        fields = {
                            f.name: ground_field(f, chunk) for f in proposed.fields
                        }
                        if len(fields) != len(proposed.fields):
                            raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION)
                        order = (
                            chunk.fragments[int(proposed.anchor.ref[1:])].order,
                            anchor.start,
                        )
                        grounded.append(
                            GroundedEntity(
                                key,
                                proposed.entity_type,
                                anchor,
                                order,
                                proposed.parent_entity_id,
                                fields,
                            )
                        )
                    for entity in grounded:
                        if entity.parent is not None:
                            if entity.parent not in aliases:
                                raise LLMProviderError(
                                    LLMErrorCode.UNKNOWN_SOURCE_REFERENCE
                                )
                            entity.parent = aliases[entity.parent]
                    for alias in suggestion.unresolved_refs:
                        index = int(alias[1:])
                        if index >= len(chunk.fragments):
                            raise LLMProviderError(
                                LLMErrorCode.UNKNOWN_SOURCE_REFERENCE
                            )
                        explicit_unresolved.add(chunk.fragments[index].ref)
                    explicit_unresolved.update(
                        fragment.ref
                        for i, fragment in enumerate(chunk.fragments)
                        if f"r{i}" not in used_aliases
                    )
                    for entity in grounded:
                        current = entities.get(entity.key)
                        if current is None:
                            if len(entities) >= self.policy.max_document_entities:
                                issue(
                                    "SEMANTIC_ENTITY_LIMIT", (entity.anchor.source_ref,)
                                )
                                continue
                            entities[entity.key] = entity
                            continue
                        if (
                            current.entity_type != entity.entity_type
                            or current.parent != entity.parent
                            or any(
                                name in current.fields and current.fields[name] != value
                                for name, value in entity.fields.items()
                            )
                        ):
                            conflicts.add(entity.key)
                            issue(
                                "SEMANTIC_ENTITY_CONFLICT", (entity.anchor.source_ref,)
                            )
                        else:
                            matched += 1
                            current.fields.update(entity.fields)
                            if len(current.fields) > 16:
                                conflicts.add(entity.key)
                                issue(
                                    "SEMANTIC_FIELD_LIMIT", (entity.anchor.source_ref,)
                                )
                except LLMProviderError as error:
                    issue(error.error_code, refs)
                    if error.error_code in {
                        LLMErrorCode.BUDGET_EXCEEDED,
                        LLMErrorCode.UNSAFE_CONTENT,
                        LLMErrorCode.POLICY_DENIED,
                        LLMErrorCode.TIMEOUT,
                        LLMErrorCode.RATE_LIMIT,
                        LLMErrorCode.UNAVAILABLE,
                    }:
                        exhausted = True
                await asyncio.sleep(0)
        finally:
            await chunks.aclose()
        if exhausted:
            issue("SEMANTIC_PARTIAL_DOCUMENT", ())
        field_types: dict[tuple[str, str], tuple[str, str]] = {}
        for entity in entities.values():
            for field in entity.fields.values():
                type_key = (entity.entity_type, field.name)
                previous = field_types.get(type_key)
                if previous is not None and previous[0] != field.semantic_type:
                    conflicts.update((entity.key, previous[1]))
                    issue(
                        "SEMANTIC_TYPE_CONFLICT",
                        (
                            entity.anchor.source_ref,
                            entities[previous[1]].anchor.source_ref,
                        ),
                    )
                else:
                    field_types[type_key] = (field.semantic_type, entity.key)
        # Conflicting parents/cycles не оставляют orphan children в принятом plan.
        for entity in entities.values():
            visited = {entity.key}
            parent = entity.parent
            while parent is not None:
                if parent in visited or parent in conflicts or parent not in entities:
                    conflicts.add(entity.key)
                    issue("SEMANTIC_PARENT_UNRESOLVED", (entity.anchor.source_ref,))
                    break
                visited.add(parent)
                parent = entities[parent].parent
        accepted = sorted(
            (e for key, e in entities.items() if key not in conflicts),
            key=lambda e: (e.order, e.key),
        )
        accepted_keys = {e.key for e in accepted}
        accepted = [
            e for e in accepted if e.parent is None or e.parent in accepted_keys
        ]
        try:
            plan = (
                self._plan(accepted, profile, manifest, generations)
                if accepted
                else None
            )
        except (ValueError, TypeError, RecursionError):
            plan = None
            issue("SEMANTIC_PLAN_LIMIT_OR_CONFLICT", ())
        covered = set(plan.block_refs) if plan is not None else set()
        unresolved = tuple(
            ref
            for ref in source.refs
            if ref not in covered or ref in explicit_unresolved
        )
        if unresolved or source.omitted:
            issue("SEMANTIC_UNRESOLVED_SOURCE", unresolved[:64])
        return DocumentAnalysis(
            plan,
            tuple(issues),
            tuple(source.refs),
            unresolved,
            len(unresolved) + source.omitted,
            Decimal(1) if matched else Decimal("0.85"),
        )

    def _plan(
        self,
        entities: list[GroundedEntity],
        profile: StructureProfile,
        manifest: ExtractedDatasetManifest,
        generations: list[LLMPlanProvenance],
    ) -> DocumentParsePlan:
        fields: list[ParseField] = []
        definitions: list[ParseEntity] = []
        by_key = {e.key: e for e in entities}
        for entity in entities:
            field_ids = []
            for name, field in sorted(entity.fields.items()):
                identifier = f"f_{entity.key}_{name}"
                field_ids.append(identifier)
                fields.append(
                    ParseField(
                        field_id=identifier,
                        semantic_name=name,
                        semantic_type=field.semantic_type,
                        source_refs=tuple(
                            dict.fromkeys(span.source_ref for span in field.spans)
                        ),
                        selector=DocumentSpanSelector(spans=field.spans),
                    )
                )
            root = entity
            while root.parent is not None:
                root = by_key[root.parent]
            definitions.append(
                ParseEntity(
                    entity_id="e_" + entity.key,
                    entity_type=entity.entity_type,
                    field_ids=tuple(field_ids),
                    parent_entity_id="e_" + entity.parent if entity.parent else None,
                    grouping=DocumentSpanGrouping(
                        record_id="r_" + root.key, anchor=entity.anchor
                    ),
                )
            )
        refs = tuple(
            dict.fromkeys(
                (
                    *[e.anchor.source_ref for e in entities],
                    *[ref for f in fields for ref in f.source_refs],
                )
            )
        )
        return DocumentParsePlan(
            plan_id="document_"
            + canonical_sha256_value(tuple(d.canonical_json() for d in definitions))[
                -24:
            ],
            schema_version="1.2.0",
            revision=1,
            source_fingerprint=manifest.source.source_fingerprint,
            extraction_fingerprint=manifest.extraction_fingerprint,
            profile_fingerprint=profile.profile_fingerprint,
            confidence=Decimal(0),
            producer=ProducerMetadata(
                component_id="document_entity_extractor",
                component_version="1.0.0",
                sdk_version="0.3.0",
            ),
            semantic_generations=tuple(generations),
            fields=tuple(fields),
            entities=tuple(definitions),
            evidence=refs,
            block_refs=refs,
        )
