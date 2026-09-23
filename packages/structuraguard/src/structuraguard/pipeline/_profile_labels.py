"""Исходные имена из уже проверенного плана и физического snapshot."""

from structuraguard.contracts.analysis import TreePathOperation
from structuraguard.contracts.common import StringScalar
from structuraguard.contracts.normalized import SemanticFieldRef
from structuraguard.contracts.parsing import (
    TabularColumnSelector,
    TabularParsePlan,
    TreePathSelector,
)
from structuraguard.contracts.profiling import ProfileLabel

from .source import NormalizedData


def source_labels(source: NormalizedData) -> tuple[ProfileLabel, ...]:
    """Сохранить точные имена без переименования полей или разбора их ID.

    Вызывается после проверки source/plan fingerprints в orchestrator. Название
    остаётся недоверенным evidence; limits labels не допускают его усечения.
    """
    plan = source.plan.plan
    names: dict[str, str] = {}
    if isinstance(plan, TabularParsePlan) and plan.header_row is not None:
        headers: dict[int, str | None] = {}
        selected = {
            field.selector.column_index
            for field in plan.fields
            if isinstance(field.selector, TabularColumnSelector)
        }
        for batch in source.source.batches:
            source.source._run.resources.check_deadline()
            if batch.extraction_id != plan.table_ref.extraction_id:
                continue
            for table in batch.tables:
                if table.table_id != plan.table_ref.local_id:
                    continue
                for cell in table.cells:
                    if (
                        cell.row_index != plan.header_row
                        or cell.column_index not in selected
                    ):
                        continue
                    raw = cell.value.raw_value
                    text = raw.value if isinstance(raw, StringScalar) else None
                    previous = headers.setdefault(cell.column_index, text)
                    if previous != text:
                        headers[cell.column_index] = None
        for field in plan.fields:
            if isinstance(field.selector, TabularColumnSelector):
                text = headers.get(field.selector.column_index)
                if text is not None:
                    names[field.field_id] = text
    else:
        for field in plan.fields:
            selector = field.selector
            if not isinstance(selector, TreePathSelector):
                continue
            if selector.value_source != "node_value" or len(selector.steps) != 1:
                continue
            step = selector.steps[0]
            if step.operation is TreePathOperation.KEY and step.occurrence == 0:
                assert step.name is not None
                names[field.field_id] = step.name

    fields = {field.field_id: field for field in plan.fields}
    labels: dict[SemanticFieldRef, str | None] = {}
    for entity in plan.entities:
        for field_id in entity.field_ids:
            text = names.get(field_id)
            if not text or not text.strip() or len(text.encode("utf-8")) > 256:
                text = None
            ref = SemanticFieldRef(
                entity_type=entity.entity_type,
                field_name=fields[field_id].semantic_name,
            )
            previous = labels.setdefault(ref, text)
            if previous != text:
                labels[ref] = None
    return tuple(
        ProfileLabel(
            field=ref, kind="source_name", text=text, origin="observed_location"
        )
        for ref, text in sorted(
            labels.items(), key=lambda item: (item[0].entity_type, item[0].field_name)
        )
        if text is not None
    )
